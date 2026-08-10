"""Central configuration for the experiment-paper writing framework.

This is a standalone project (sibling of `survey/`). Unlike the survey engine,
its content source is a directory of experiment results (CSV/JSON/logs/plots)
rather than a literature-note library. Paths below are all project-relative so
the framework can be copied anywhere.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

# ── Project layout (all relative to this file's directory) ──────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT_ROOT / "data"            # user-provided experiment results
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"  # section drafts / reviews / finals
REFERENCES_ROOT = PROJECT_ROOT / "references"  # bibliography + reading notes
REFERENCE_INDEX_PATH = REFERENCES_ROOT / "index.md"
BIBLIOGRAPHY_PATH = REFERENCES_ROOT / "bibliography.md"

# ── The idea document (highest-priority input, read by EVERY agent) ──────
# One global file the user writes: the novelty, the mechanism, the method
# design, the claims. This — not the results store — is what a Method /
# Introduction / Related Work chapter is written from. Experiment numbers only
# support the idea; they are not the contribution. Every chapter reads the SAME
# idea.md, so the novelty is stated identically across the paper. It is injected
# as the first instruction of every stage prompt (idea is never copied into a
# pack or paraphrased — paraphrasing upstream is how a paper's claims drift).
IDEA_PATH = Path(os.getenv("IDEA_PATH") or (PROJECT_ROOT / "idea.md"))

# ── The data index (Manager-built navigation over data/) ─────────────────
# data/ holds the raw experiment files (CSV/JSON/logs/plots). Agents drafting
# data-family chapters (results/experiments/ablation) navigate them via this
# index — a three-level map (experiment → result → specific value) the Manager
# writes during `--init`. Once it exists it is NOT overwritten (the author may
# have edited it). The number gate still reads the raw data/ as ground truth;
# this index is only for the agents' navigation, never for verification.
DATA_INDEX_PATH = Path(os.getenv("DATA_INDEX_PATH") or (DATA_ROOT / "data-index.md"))

# Kept as a str for parity with the survey engine's tool code.
PAPER_ROOT = str(WORKSPACE_ROOT)

# ── Mode ────────────────────────────────────────────────────────────────
# "experiment": content source is data/ (results). "survey": content source is
# references/ notes (parity with the original engine, for reuse/testing).
PAPER_MODE = os.getenv("PAPER_MODE", "experiment")

# ── Per-agent model config (provider + model + key + base, each independent) ─
# 模型调用框架：OpenAI 走 smolagents OpenAIModel，Claude 走 AnthropicModel 直连 /v1/messages。
# provider 由显式字段决定，不再靠 model_id 前缀推断；reasoning_level 统一 6 档语义，
# 由 agents.model_capabilities 按模型能力翻译成 provider 原生参数（不支持则显式报错）。
DRAFT_PROVIDER = os.getenv("DRAFT_PROVIDER", "anthropic")
DRAFT_MODEL = os.getenv("DRAFT_MODEL", "claude-opus-4-6")
DRAFT_API_KEY = os.getenv("DRAFT_API_KEY", "")
DRAFT_API_BASE = os.getenv("DRAFT_API_BASE", "")
DRAFT_REASONING_LEVEL = os.getenv("DRAFT_REASONING_LEVEL", "max")

REVIEW_PROVIDER = os.getenv("REVIEW_PROVIDER", "openai")
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "gpt-5")
REVIEW_API_KEY = os.getenv("REVIEW_API_KEY", "")
REVIEW_API_BASE = os.getenv("REVIEW_API_BASE", "")
REVIEW_REASONING_LEVEL = os.getenv("REVIEW_REASONING_LEVEL", "high")

MANAGER_PROVIDER = os.getenv("MANAGER_PROVIDER", "anthropic")
MANAGER_MODEL = os.getenv("MANAGER_MODEL", "claude-opus-4-6")
MANAGER_API_KEY = os.getenv("MANAGER_API_KEY", "")
MANAGER_API_BASE = os.getenv("MANAGER_API_BASE", "")
MANAGER_REASONING_LEVEL = os.getenv("MANAGER_REASONING_LEVEL", "high")

# ── 模型调用运行时策略（timeout/retry）─────────────────────────────────────
# 排障期默认 max_retries=0，避免一次失败被串行放大成多次长等待；稳定后可调 1。
MODEL_TIMEOUT = os.getenv("MODEL_TIMEOUT")  # 留空 → 按 provider 默认（OpenAI 300 / Claude 600）
MODEL_MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "0"))

# ── Retrieval LLM (the strong web-search model, e.g. grok) ──────────────
# Used by the two-tier retrieval: notes/bib first, this model for web lookup.
# Left blank disables the web tier; the framework still works notes-only.
RETRIEVAL_MODEL = os.getenv("RETRIEVAL_MODEL", "")
RETRIEVAL_API_KEY = os.getenv("RETRIEVAL_API_KEY", "")
RETRIEVAL_API_BASE = os.getenv("RETRIEVAL_API_BASE", "")

MAX_STEPS_AGENT = int(os.getenv("MAX_STEPS_AGENT", "15"))
MAX_STEPS_MANAGER = int(os.getenv("MAX_STEPS_MANAGER", "20"))

# ── Convergence loop (Stage 3) ──────────────────────────────────────────
# Keep revising until the first review's MUST FIX checklist is fully resolved,
# capped at MAX_REVISION_ROUNDS to avoid burning tokens on genuinely unfixable
# issues (missing data, etc.). The hard gate is "all MUST FIX resolved", not
# the score; REVIEW_SCORE_THRESHOLD only records the bar we report against.
MAX_REVISION_ROUNDS = int(os.getenv("MAX_REVISION_ROUNDS", "4"))
REVIEW_SCORE_THRESHOLD = float(os.getenv("REVIEW_SCORE_THRESHOLD", "4.0"))

# ── Citation integrity ──────────────────────────────────────────────────
# Auto-supplement missing citations only from the local bibliography by
# default; web-sourced citations must carry a verifiable URL before insertion.
AUTO_CITE_WEB = os.getenv("AUTO_CITE_WEB", "false").lower() == "true"


def _model_timeout() -> float | None:
    """解析 MODEL_TIMEOUT：留空返回 None（由 router 按 provider 取默认）。"""
    if MODEL_TIMEOUT is None or MODEL_TIMEOUT == "":
        return None
    try:
        return float(MODEL_TIMEOUT)
    except (TypeError, ValueError):
        return None


def get_draft_model():
    """Draft Agent — drafting and revision（最大推理深度）。"""
    from agents.model_router import build_model  # 懒加载：config 顶层不引入 smolagents

    return build_model(
        DRAFT_PROVIDER, DRAFT_MODEL, DRAFT_API_KEY, DRAFT_API_BASE,
        DRAFT_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
    )


def get_review_model():
    """Review Agent — critique, verify, finalize（高推理深度）。"""
    from agents.model_router import build_model

    return build_model(
        REVIEW_PROVIDER, REVIEW_MODEL, REVIEW_API_KEY, REVIEW_API_BASE,
        REVIEW_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
    )


def get_manager_model():
    """Manager Agent — Stage-1 planning and orchestration（高推理深度）。"""
    from agents.model_router import build_model

    return build_model(
        MANAGER_PROVIDER, MANAGER_MODEL, MANAGER_API_KEY, MANAGER_API_BASE,
        MANAGER_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
    )
