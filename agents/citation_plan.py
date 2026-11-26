"""引用分配表：作者审阅 Markdown，程序校验位置、覆盖率和输入版本。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

HEADERS = ("key", "chapter", "section", "role", "status", "reason", "source")


def file_sha(path) -> str:
    """缺失文件与空文件使用不同指纹。"""
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def paths() -> tuple[Path, Path]:
    import config
    return Path(config.PROJECT_ROOT) / "citation-plan.md", Path(config.INDEX_DIR) / "citation-plan.json"


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_citation_plan(path) -> list[dict]:
    """严格解析七列表格，格式错误不能被当作空表继续运行。"""
    rows, header_found = [], False
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
    for number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if tuple(cells) == HEADERS and not header_found:
            header_found = True
            continue
        if cells and all(cell and not cell.strip("-: ") for cell in cells):
            continue
        if not header_found or len(cells) != len(HEADERS):
            raise ValueError(f"引用分配表第 {number} 行必须使用七列 {', '.join(HEADERS)}")
        row = dict(zip(HEADERS, cells))
        try:
            row["chapter"], row["section"] = int(row["chapter"]), int(row["section"])
        except ValueError as exc:
            raise ValueError(f"{row['key']}: chapter 和 section 必须是整数（第 {number} 行）") from exc
        rows.append(row)
    if not header_found:
        raise ValueError("引用分配表缺少七列表头")
    return rows


def render_plan(rows) -> str:
    lines = ["# Citation plan", "",
             "<!-- 每篇文献必须有分配。omit 需写理由；修改后运行 --init --force 刷新 brief.md。 -->", "",
             "| " + " | ".join(HEADERS) + " |", "|" + "|".join(["---"] * len(HEADERS)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(key, "")).replace("|", "/").replace("\n", " ")
                                       for key in HEADERS) + " |")
    return "\n".join(lines) + "\n"


def validate_citation_plan(rows, bib_keys, chapters) -> list[str]:
    by_number = {ch["number"]: ch for ch in chapters}
    errors, covered, seen = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            errors.append("引用分配行必须是对象")
            continue
        key, number, section = row.get("key"), row.get("chapter"), row.get("section")
        if key not in bib_keys:
            errors.append(f"{key}: 未知文献 key")
        else:
            covered.add(key)
        chapter = by_number.get(number) if isinstance(number, int) and not isinstance(number, bool) else None
        if chapter is None:
            errors.append(f"{key}: 章号 {number} 不存在")
        elif chapter["type"] in {"abstract", "conclusion"}:
            errors.append(f"{key}: 第 {number} 章为 {chapter['type']}，不能分配引用")
        elif (not isinstance(section, int) or isinstance(section, bool)
              or not 1 <= section <= max(1, len(chapter.get("sections", [])))):
            errors.append(f"{key}: 第 {number} 章小节 {section} 越界")
        if row.get("status") not in {"cite", "omit"}:
            errors.append(f"{key}: status 必须是 cite 或 omit")
        elif row["status"] == "omit" and not str(row.get("reason", "")).strip():
            errors.append(f"{key}: omit 必须填写 reason")
        if all(isinstance(value, (int, str)) for value in (key, number, section)):
            ident = (key, number, section)
            if ident in seen:
                errors.append(f"{key}: 第 {number} 章第 {section} 节重复分配")
            seen.add(ident)
    errors += [f"{key}: 缺少引用分配" for key in sorted(set(bib_keys) - covered)]
    return errors


def prefill_from_outline(outline_chapters, bib_keys) -> list[dict]:
    from .materials import keys_in_text
    rows = []
    for chapter in outline_chapters:
        if chapter["type"] in {"abstract", "conclusion"}:
            continue
        for section in chapter.get("sections", []):
            for key in keys_in_text("\n".join(section.get("bullets", [])), valid=set(bib_keys)):
                rows.append(dict(key=key, chapter=chapter["number"], section=section["number"],
                                 role=next(bullet for bullet in section["bullets"]
                                           if key in keys_in_text(bullet, valid=set(bib_keys))).replace("|", "/"),
                                 status="cite", reason="", source="outline"))
    return rows


def build_plan_prompt(chapters, bib_keys, prefilled) -> str:
    remaining = sorted(set(bib_keys) - {row["key"] for row in prefilled})
    locations = "\n".join(f"{ch['number']}. {ch['title']} ({ch['type']}): "
                          + ", ".join(f"{s['number']}={s['title']}" for s in ch.get("sections", []))
                          for ch in chapters)
    return (
        "Allocate EVERY author-provided reference to a chapter and section. Return citation-plan.md "
        "using exactly the seven columns shown below. Preserve all prefilled rows verbatim. "
        "For each remaining key select the best chapter and section and explain its scientific role. "
        "Follow the author's citation-position instructions in idea.md first. Prefer citing every "
        "paper. Use omit ONLY if no relevant placement exists and give a specific reason. "
        "Never allocate to abstract or conclusion, including omit rows. Use integer chapter/section "
        "numbers. A chapter without subsections uses section 1. source=planner for your new rows. "
        "Do not invent keys or change the outline. No code fences.\n\n"
        f"Locations:\n{locations}\n\nRemaining keys:\n{', '.join(remaining)}\n\n"
        f"Prefilled table and required output format:\n{render_plan(prefilled)}"
    )


def write_plan_json(rows, bib_sha, outline_sha, path):
    _write(path, json.dumps(dict(version=1, bib_sha=bib_sha, outline_sha=outline_sha, rows=rows),
                           ensure_ascii=False, indent=2) + "\n")


def required_for_chapter(rows, chapter_number):
    return sorted((row for row in rows if row["status"] == "cite" and row["chapter"] == chapter_number),
                  key=lambda row: (row["section"], row["key"]))


def load_levels():
    import config
    path = Path(config.INDEX_DIR) / "ref-levels.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def required_block(chapter, rows=None) -> str:
    """此块同时进入 brief 和该章指纹，改一章的分配不影响其他章。"""
    if rows is None:
        md, _ = paths()
        rows = parse_citation_plan(md) if md.is_file() else []
    required = required_for_chapter(rows, chapter.get("number"))
    lines = ["## Required Citations", "",
             r"Every key below is author-provided and MUST be cited in its listed section at least once "
             r"(a grouped \cite{a,b} counts). Describe each per its level: A-level 2-3 sentences, "
             "B-level one clause, C-level citation only. Do not report numbers from other papers "
             "unless the role says so.", ""]
    if not required:
        return "\n".join(lines + ["(none)", ""])
    levels = load_levels()
    lines += ["| key | level | section | role |", "|---|---|---|---|"]
    lines += [f"| {r['key']} | {levels.get(r['key'], 'B')} | {r['section']} | {r['role']} |"
              for r in required]
    return "\n".join(lines) + "\n"


def required_from_brief(folder_path) -> list[dict]:
    """读取本章实际收到的必引块和小节标题，不依赖文件夹命名截断规则。"""
    import re
    path = Path(folder_path) / "brief.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    titles = {int(n): title for n, title in re.findall(r"(?m)^(\d+)\.\s+\*\*(.+?)\*\*", text)}
    block = re.search(r"(?ms)^## Required Citations\s*\n(.*?)(?=^## |\Z)", text)
    rows = []
    if block:
        for line in block[1].splitlines():
            cells = [part.strip() for part in line.strip().strip("|").split("|")]
            if len(cells) == 4 and cells[2].isdigit():
                rows.append(dict(key=cells[0], level=cells[1], section=int(cells[2]), role=cells[3],
                                 section_title=titles.get(int(cells[2]), ""),
                                 section_count=len(titles)))
    return rows


def sync_plan(chapters):
    """--init 校验作者版再更新程序版。旧项目先显式生成分配表。"""
    import config
    from .references import citable_keys
    keys = citable_keys(config.REFS_BIB_PATH)
    md, dest = paths()
    if not md.is_file():
        if keys:
            raise ValueError("缺少 citation-plan.md，请先运行 --plan-citations")
        _write(md, render_plan([]))
    rows = parse_citation_plan(md)
    errors = validate_citation_plan(rows, keys, chapters)
    if errors:
        raise ValueError("\n".join(errors))
    write_plan_json(rows, file_sha(config.REFS_BIB_PATH), file_sha(config.WORK_ROOT / "outline.md"), dest)
    return rows


def check_plan_current(chapters) -> list[str]:
    """--go 的版本门禁；作者只改分配行时由 --init 同步，源输入变化需重新分配。"""
    import config
    from .references import citable_keys
    if not citable_keys(config.REFS_BIB_PATH):
        return []
    md, path = paths()
    try:
        if not md.is_file() or not path.is_file():
            return ["引用分配表缺失，请先运行 --plan-citations（已有作者版时运行 --init）"]
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("version") != 1 or data.get("bib_sha") != file_sha(config.REFS_BIB_PATH)
                or data.get("outline_sha") != file_sha(config.WORK_ROOT / "outline.md")):
            return ["引用分配表所依据的 refs.bib / outline.md 已变化，请保留旧表后重新运行 --plan-citations"]
        return validate_citation_plan(parse_citation_plan(md), citable_keys(config.REFS_BIB_PATH), chapters)
    except (OSError, ValueError, AttributeError) as exc:
        return [f"引用分配表无效，请运行 --plan-citations：{exc}"]


def ensure_input_required(path, required):
    """只补备料表中的位置与作用，不插入任何正文引用。"""
    import re
    path = Path(path)
    if not path.is_file() or not required:
        return
    text = path.read_text(encoding="utf-8")
    header = re.search(r"(?im)^\|\s*key\s*\|\s*level\s*\|[^\n]+\n\|[- :|]+\|[ \t]*\n", text)
    end = header.end() if header else len(text)
    existing = set()
    if header:
        while end < len(text) and text[end:].startswith("|"):
            line, _, _ = text[end:].partition("\n")
            existing.add(line.split("|")[1].strip())
            end += len(line) + (end + len(line) < len(text))
    missing = []
    for row in required:
        if row["key"] not in existing:
            missing.append(f"| {row['key']} | {row['level']} | {row['section']} | "
                           f"{row['role']} (added by citation plan) |")
            existing.add(row["key"])
    if missing:
        addition = "\n".join(missing) + "\n"
        if not header:
            addition = ("\n\n## Required reference material\n\n| key | level | section(s) | use in this chapter |\n"
                        "|---|---|---|---|\n" + addition)
        _write(path, text[:end] + addition + text[end:])
