"""Skill 受控装载 + run-manifest 指纹缓存(移植自 MathModel_Paper 演进点③)。

旧机制的两个洞:
1. Skill 靠 Agent 运行期自助 read_file(全局指令只列了文件名) —— 装没装、
   装的哪份,编排器一无所知,也无法审计;
2. resume 判据是"文件存在即跳过" —— 只校验 brief 的 outline 指纹。改了
   prompts.py 或 skills/*.md 后,已存在的 evidence-pack/draft/review 仍被
   静默复用:旧提示词的产物冒充新跑,日志里全部显示 skip|exists。

新机制:
- 固定注册表 SKILL_REGISTRY:角色 → 允许的 skill 文件名白名单;
- load_skills() 读 skills/ 下的白名单文件,返回 (注入文本, SHA256 指纹);
- run_manifest:每个"存在即跳过"产物在生成时记录指纹 = brief outline 指纹
  + 阶段名 + prompts.py/skills 哈希 + 作者输入。模型组仅记录来源，不参与指纹。重跑时不匹配 → 视为
  陈旧,硬停并列出(不删文件,沿用 warn_stale_route_artifacts 的语义)。

指纹存 workspace/<章>/.stage-manifest.json: {artifact: {"fingerprint": ..., "mtime": ...}}。
文件存在但不在 manifest 里(手工造/旧版本跑的) → 视为陈旧,行为保守:
宁可要求重跑,不冒充新跑。
"""

import hashlib
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.py"

MANIFEST_FILENAME = ".stage-manifest.json"
FINGERPRINT_VERSION = 2

# 阶段提示词版本。prompts.py 已按内容哈希进指纹,但各阶段的任务提示词写在
# orchestrator.py / evidence_mining.py 里——哈希整个代码文件会让任何无关改动都
# 作废全部产物,所以改用手动版本号:**修改阶段任务提示词时把它加 1**,旧产物
# 随之判陈旧。
#   1 → 2:Stage 4 定稿改为只基于 draft-v2(不再与 draft-v1 合并)。
#   2 → 3:正面写法(不写没做什么,局限只在最后一章);证据挖掘视角改为找亮点。
STAGE_PROMPT_VERSION = 3  # top-venue-craft.md 进白名单后由 skills 哈希自动作废旧产物

# 各角色允许装载的 Skill 白名单。不在白名单里的 skills/ 文件编排器不注入,
# Agent 的自助 read_file 仍可用(向后兼容),但指纹只覆盖白名单文件。
SKILL_REGISTRY: dict[str, tuple[str, ...]] = {
    "draft": (
        "top-venue-craft.md",
        "writing-style.md",
        "figure-table-placeholder.md",
        "math-formula.md",
        "experiment-writing.md",
    ),
    "review": (
        "review-rubric.md",
    ),
    "manager": (),
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skills_fingerprint(roles: tuple[str, ...] = ("draft", "review", "manager")) -> str:
    """白名单 skill 文件 + prompts.py 的联合内容指纹。

    任何被注入的 skill 内容或提示词模板变化 → 指纹变 → 旧产物判陈旧。
    文件缺失不影响计算(空内容哈希),registry 变更同理不在此捕获——
    registry 是代码,改它必然改这个文件,自身就在指纹里。
    """
    parts = [_sha256_file(PROMPTS_PATH)]
    for role in roles:
        for name in SKILL_REGISTRY.get(role, ()):
            path = SKILLS_DIR / name
            digest = _sha256_file(path) if path.is_file() else _sha256_text("")
            parts.append(f"{role}:{name}:{digest}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def load_skills(role: str) -> tuple[str, str]:
    """读白名单 skill 文件,拼成注入块。返回 (注入文本, 该角色指纹)。

    缺失的文件跳过并注明 —— skills/ 是仓库内容,正常都该在;缺席说明仓库
    不完整,注明比硬停合适(装载矩阵是软约束,指纹缓存才是硬约束)。
    """
    names = SKILL_REGISTRY.get(role, ())
    blocks = []
    digests = []
    for name in names:
        path = SKILLS_DIR / name
        if not path.is_file():
            blocks.append(f"<!-- skill {name} missing from skills/ -->")
            digests.append(f"{name}:missing")
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        blocks.append(f"<!-- === skill: {name} === -->\n{content}")
        digests.append(f"{name}:{_sha256_text(content)[:12]}")
    text = "\n\n".join(blocks)
    fingerprint = hashlib.sha256(("|".join(digests)).encode("utf-8")).hexdigest()[:12]
    return text, fingerprint


def build_skill_injection(role: str) -> str:
    """生成注入到该角色任务提示词末尾的 Skill 块。

    只在非空时返回非空串;manager 白名单为空 → 返回 ""。
    替代旧行为(Agent 自助 read_file):装载变成确定性的、可审计的、进指纹的。
    """
    text, _fp = load_skills(role)
    if not text:
        return ""
    return (
        f"\n=== METHODOLOGY SKILLS (pre-loaded; do not re-read these files) ===\n"
        f"{text}\n"
    )


# ── 作者输入指纹:idea.md / data/ 变了,旧产物不能冒充新跑 ─────────────
def _sha256_path(path) -> str:
    p = Path(path)
    return _sha256_file(p)[:16] if p.is_file() else "missing"


def inputs_fingerprint(family: str, idea_path=None, data_root=None) -> dict:
    """本章依赖的作者输入的内容指纹。返回 {"idea": ..., ["data": ...]}。

    - 所有章节都读 idea.md(idea_clause 注入每个改写正文的阶段)→ 都含 idea;
    - data/mixed 章节的正文引用结果 → 再含 data。idea 章(方法/引言)不因
      实验结果变动而整章作废。
    data 取**数值结果库**(load_results_store 压平后)的规范化哈希,而不是原始
    文件:data/ 下的 res-ids.md / data-index.md 由程序写入,哈希原始目录会让
    指纹自己作废自己;只改图片/日志也不应让正文作废。
    """
    if idea_path is None or data_root is None:
        from config import IDEA_PATH, DATA_ROOT
        idea_path = IDEA_PATH if idea_path is None else idea_path
        data_root = DATA_ROOT if data_root is None else data_root
    inputs = {"idea": _sha256_path(idea_path)}
    import config
    if config.HAS_PROJECT:
        # 文献库:bib 条目 + 一句话总结 + 有无笔记/原文都在文献索引里。(笔记正文细节变化
        # 只影响用到它的章,目前不逐章追踪——见 TODO。)
        notes = sorted(Path(config.INDEX_DIR, "refs").glob("*.md")) if Path(config.INDEX_DIR, "refs").is_dir() else []
        inputs["refs"] = _sha256_text(_sha256_path(config.REF_INDEX_PATH) + "".join(
            _sha256_file(p) for p in notes))[:16]   # 索引 + 全部过滤版笔记:任一篇变了都让已完成的章判陈旧
    if family in ("data", "mixed"):
        from .number_gate import load_results_store, load_results_raw
        store = load_results_store(str(data_root))
        # 数值 + CSV 单元格原文(76.10 → 76.100 数值不变,但渲染出的表和正文精度会变)
        canon = json.dumps([store, load_results_raw(str(data_root))], sort_keys=True, ensure_ascii=False)
        inputs["data"] = _sha256_text(canon)[:16]
        inputs["settings"] = _sha256_path(Path(data_root) / config.SETTINGS_FILENAME)
        if config.HAS_PROJECT:
            inputs["code"] = _sha256_path(config.CODE_INDEX_PATH)
    return inputs


_INPUT_LABELS = {"idea": "idea.md", "data": "结果表", "refs": "文献库",
                 "settings": "实验设置.md", "code": "代码索引"}


def changed_inputs(folder_path: str, artifacts: list[str], inputs: dict) -> list[str]:
    """对比 manifest 里记录的输入指纹,返回已变化的输入名(如 ["idea.md"])。

    只用于给硬停信息说明原因;判陈旧本身仍由 stale_stage_artifacts 决定。
    """
    manifest = load_manifest(folder_path)
    changed = set()
    for name in artifacts:
        entry = manifest.get(name)
        old = entry.get("inputs") if isinstance(entry, dict) else None
        if not isinstance(old, dict):
            continue
        for k, v in inputs.items():
            if old.get(k) != v:
                changed.add(_INPUT_LABELS.get(k, k))
    return sorted(changed)


# ── run manifest:产物级指纹记账 ─────────────────────────────────────────
def _manifest_path(folder_path: str) -> Path:
    return Path(folder_path) / MANIFEST_FILENAME


def load_manifest(folder_path: str) -> dict:
    path = _manifest_path(folder_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_manifest(folder_path: str, manifest: dict) -> None:
    path = _manifest_path(folder_path)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record_stage_fingerprint(folder_path: str, artifact: str,
                             brief_fingerprint: str, stage: str,
                             model_group: str = "",
                             inputs: dict | None = None) -> None:
    """按当前文件内容与阶段指纹记账；两者都未变时保留原条目及模型来源。

    缺少内容哈希的旧条目需要重记；内容哈希只用于记账，不改变手改终稿的核验流程。
    """
    manifest = load_manifest(folder_path)
    fingerprint = stage_fingerprint(brief_fingerprint, stage, model_group, inputs)
    content_sha256 = _sha256_file(Path(folder_path) / artifact)
    previous = manifest.get(artifact)
    if isinstance(previous, dict) and previous.get("content_sha256") == content_sha256 \
            and previous.get("fingerprint") == fingerprint \
            and previous.get("fingerprint_version") == FINGERPRINT_VERSION:
        return
    manifest[artifact] = {
        "fingerprint": fingerprint,
        "content_sha256": content_sha256,
        "fingerprint_version": FINGERPRINT_VERSION,
        "stage": stage,
        "model_group": model_group,
        "inputs": dict(inputs or {}),
    }
    save_manifest(folder_path, manifest)


def stage_fingerprint(brief_fingerprint: str, stage: str,
                      model_group: str = "", inputs: dict | None = None) -> str:
    """阶段指纹 = brief 指纹 + 阶段名 + prompt/skill 内容指纹 + 阶段提示词版本
    + 作者输入(idea.md / data/)指纹。

    model_group 参数保留调用兼容性，但不进入指纹；换模型可继续复用已完成内容。
    生成来源另记 manifest.model_group，跳过旧产物时不改写其来源。
    """
    inputs_part = json.dumps(inputs or {}, sort_keys=True)
    payload = "|".join([brief_fingerprint, stage, skills_fingerprint(),
                        f"pv{STAGE_PROMPT_VERSION}", inputs_part])
    return _sha256_text(payload)[:16]


def stale_stage_artifacts(folder_path: str, artifacts: list[str],
                          brief_fingerprint: str,
                          model_groups: dict[str, str] | None = None,
                          inputs: dict | None = None) -> list[str]:
    """列出"存在但指纹不符/未记账"的产物。

    model_groups 参数保留调用兼容性，不参与陈旧判定。
    旧版账本没有独立保存模型来源，无法安全剥离其哈希中的模型维，保守判陈旧一次。
    inputs: inputs_fingerprint() 的结果,必须与记账时同口径。
    返回陈旧文件名列表;不存在或指纹相符的产物不在列表里。
    """
    manifest = load_manifest(folder_path)
    stale = []
    for name in artifacts:
        full = Path(folder_path) / name
        if not full.is_file():
            continue
        entry = manifest.get(name)
        expected = stage_fingerprint(brief_fingerprint, _stage_of(name), inputs=inputs)
        if not isinstance(entry, dict) or entry.get("fingerprint_version") != FINGERPRINT_VERSION \
                or entry.get("fingerprint") != expected:
            stale.append(name)
    return stale


def _stage_of(artifact_name: str) -> str:
    """从产物名推断阶段名(与 orchestrator 的阶段划分一一对应)。"""
    name = artifact_name
    if name.startswith("draft-v2.round-"):
        return "revise"
    if name.startswith("review-verify.round-"):
        return "verify"
    mapping = {
        "input.md": "input",
        "evidence-pack.md": "evidence",
        "draft-v1.plan.md": "plan",
        "draft-v1.md": "draft",
        "number-check.md": "number-gate",
        "review-v1.md": "review",
        "review-v1.json": "review",
        "citation-insertions.md": "citation",
        "draft-v2.md": "revise",
        "final.md": "final",
        "final.zh.md": "final",
        "cross-chapter-draft.md": "xchap",
        "cross-chapter-entries.md": "xchap",
    }
    if name.startswith("draft-v1.part-"):
        return "draft"
    return mapping.get(name, name)
