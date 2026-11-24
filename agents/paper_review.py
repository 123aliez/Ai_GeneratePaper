"""整篇一致性检查 —— 所有章写完之后、组装之前的一遍只读评审。

逐章流水线里每一章只看得到自己、邻章骨架和跨章状态:引言列的贡献后文有没有兑现、
摘要/结论里的说法和结果章是否一致、两章之间有没有重复段落,没有任何环节检查。
这里补上这一遍,但**只出问题清单,不改正文**:改正文要回到对应章重跑,改动才会
经过数字门禁与 MUST FIX 核验。

两部分:
1. 确定性部分(不调模型):跨章逐字重复的长句。
2. 模型部分(Review 模型,单轮):把各章终稿全文一次给到,按固定维度找跨章问题。
结果写 `_work/paper-review.md`,`--assemble` 把它并入 `output/待补清单.md`。
终稿没变就不重跑(文件头记录各章终稿的内容指纹)。
"""
import hashlib
import os
import re
from pathlib import Path

PAPER_REVIEW_NAME = "paper-review.md"
_SHA_RE = re.compile(r"^<!-- finals-sha: ([0-9a-f]+) -->$", re.M)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")

PAPER_REVIEW_INSTRUCTIONS = """\
You are the whole-paper consistency reviewer for an experimental research paper.
Every chapter was drafted, reviewed and verified on its own; you are the only reader
who sees all of them at once. You do NOT rewrite anything — you list problems that
only become visible across chapters, so the author can re-run the affected chapter.

Check exactly these dimensions:
1. Contribution fidelity — every contribution the introduction lists is actually delivered
   (method chapter) and evidenced (results chapters); nothing is claimed in the abstract or
   conclusion that the body does not establish; no chapter adds a contribution idea.md does
   not make.
2. Number and claim consistency — the same result is not described differently in two
   chapters (e.g. "improves" vs "matches", different magnitudes for the same comparison).
3. Duplication — passages that repeat material another chapter already covers.
4. Terminology — one concept named two ways, an abbreviation expanded twice or never,
   a symbol defined differently in two chapters.
5. Dangling references — "as shown in Section X / Table Y / the ablation" pointing at
   content no chapter contains.
6. Flow — a chapter whose opening or closing transition does not match its neighbour.

Rules:
- Report only problems you can point to in the text; quote a short phrase as evidence.
- Never judge whether the contribution is novel. Never ask for new experiments or numbers.
- Do not report problems local to one chapter — the per-chapter review already did that.
- Output Markdown only, in this shape (no preamble):
  ## Findings
  - [<chapter folder>] <dimension>: <problem, with a quoted phrase> — <which chapter to fix and how>
  (one line per finding; write "- none" if there is nothing)
"""


def _complete_text(model, messages) -> str:
    """一次完整生成的文本。优先走流式(ResilientModel 只在流式路径上做断流重连)。"""
    stream = getattr(model, "generate_stream", None)
    if callable(stream):
        return "".join(getattr(d, "content", "") or "" for d in stream(messages))
    response = model.generate(messages)
    text = getattr(response, "content", "") or ""
    return text if isinstance(text, str) else str(text)


def finals_fingerprint(chapters: list[dict], workspace) -> str:
    digest = hashlib.sha256()
    for meta in chapters:
        path = Path(workspace) / meta["folder"] / "final.md"
        digest.update(meta["folder"].encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()[:16]


def _norm_sentence(sentence: str) -> str:
    return " ".join(_WORD_RE.findall(sentence.lower()))


def duplicate_sentences(texts: dict[str, str], min_words: int = 12) -> list[str]:
    """跨章逐字(规范化后)重复的长句:[人读的描述]。同一章内的重复不报。"""
    seen: dict[str, tuple[str, str]] = {}
    out, reported = [], set()
    for folder, text in texts.items():
        for sentence in _SENT_SPLIT_RE.split(text or ""):
            sentence = sentence.strip()
            if sentence.startswith(("|", "#", "[TABLE", "[FIGURE")):
                continue
            norm = _norm_sentence(sentence)
            if len(norm.split()) < min_words:
                continue
            first = seen.get(norm)
            if first is None:
                seen[norm] = (folder, sentence)
            elif first[0] != folder and norm not in reported:
                reported.add(norm)
                out.append(f"[{folder}] duplication: sentence repeated verbatim from "
                           f"{first[0]}: \"{sentence[:140]}\"")
    return out


def build_paper_review_prompt(chapters: list[dict], workspace) -> str:
    from config import IDEA_PATH
    parts = []
    idea = Path(IDEA_PATH)
    if idea.is_file():
        parts.append(f"=== idea.md (the author's statement of the contribution) ===\n"
                     f"{idea.read_text(encoding='utf-8', errors='replace').rstrip()}")
    for meta in chapters:
        path = Path(workspace) / meta["folder"] / "final.md"
        body = (path.read_text(encoding="utf-8", errors="replace").rstrip()
                if path.is_file() else "(this chapter has no final draft yet)")
        parts.append(f"=== CHAPTER {meta.get('number', '?')}: {meta['title']} "
                     f"[{meta.get('type', '')}] (folder: {meta['folder']}) ===\n{body}")
    return ("Review the whole paper below for cross-chapter problems, following your "
            "instructions exactly.\n\n" + "\n\n".join(parts) + "\n")


def run_paper_review(model, chapters: list[dict], workspace, out_path, *,
                     force: bool = False) -> dict:
    """跑一遍整篇一致性检查,写 out_path。返回 {"status", "findings", "path"}。

    status: "skipped"(终稿没变)、"done"、"deterministic-only"(没有模型或调用失败)。
    """
    out_path = Path(out_path)
    sha = finals_fingerprint(chapters, workspace)
    if not force and out_path.is_file():
        old_text = out_path.read_text(encoding="utf-8", errors="replace")
        m = _SHA_RE.search(old_text)
        # 只有模型评审真正跑成("Status: done")才算完成。额度用完/网关失败时只留下
        # 确定性结果,下次运行必须重试模型那一半,不能因为终稿没变就跳过。
        if m and m.group(1) == sha and "\nStatus: done " in old_text:
            return {"status": "skipped", "findings": read_paper_review(out_path), "path": out_path}
    texts = {}
    for meta in chapters:
        path = Path(workspace) / meta["folder"] / "final.md"
        if path.is_file():
            texts[meta["folder"]] = path.read_text(encoding="utf-8", errors="replace")
    findings = duplicate_sentences(texts)
    status, model_note = "deterministic-only", "model review not run"
    if model is not None and texts:
        try:
            from smolagents.models import ChatMessage, MessageRole
            print(f"[Review  ] single-turn | whole-paper consistency review "
                  f"({len(texts)} chapter(s))", flush=True)
            text = _complete_text(model, [
                ChatMessage(role=MessageRole.SYSTEM, content=PAPER_REVIEW_INSTRUCTIONS),
                ChatMessage(role=MessageRole.USER,
                            content=build_paper_review_prompt(chapters, workspace)),
            ])
            lines = [ln.strip()[2:].strip() for ln in str(text).splitlines()
                     if ln.strip().startswith("- ")]
            findings += [ln for ln in lines if ln.lower() not in ("none", "none.")]
            status, model_note = "done", "model review done"
        except Exception as exc:
            model_note = f"model review failed: {type(exc).__name__}"
    lines = [f"<!-- finals-sha: {sha} -->",
             "# Whole-paper consistency review (read-only; fix by re-running the chapter)", "",
             f"Status: {status} ({model_note})", "", "## Findings", ""]
    lines += [f"- {f}" for f in findings] if findings else ["- none"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    return {"status": status, "findings": findings, "path": out_path}


def read_paper_review(path) -> list[str]:
    """已有 paper-review.md 的发现行(不含 "none")。"""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if "## Findings" not in text:
        return []
    body = text.split("## Findings", 1)[1]
    return [ln[2:].strip() for ln in body.splitlines()
            if ln.startswith("- ") and ln[2:].strip().lower() not in ("none", "none.")]
