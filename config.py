"""Central configuration for the experiment-paper writing framework.

一篇论文 = 一个项目文件夹(`python run.py --new 名字` 建立,之后所有命令带
`--project <路径>`)。run.py 在 import 本模块之前把路径放进环境变量 PAPER_PROJECT,
下面所有路径都从它派生:

    <项目>/
    ├── inbox/            作者的全部材料(Agent 只读)
    │   ├── idea.md  outline_draft.md
    │   ├── results/      主结果.csv 等 + 实验设置.md
    │   ├── references/   refs.bib + 每篇一个文件夹(笔记 + 原文)
    │   ├── code/         实验代码(只建静态索引)
    │   └── latex-template/  可选
    ├── outline.zh.md     展开后的中文大纲(作者审这个)
    ├── _work/            中间产物:索引、英文大纲、各章工作区
    └── output/           交付物

没有指定项目时(测试 / 仅 import)退回仓库根目录,路径指向不存在的 inbox/,
真正的命令入口会在 main() 里拒绝这种情况。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env", override=True)

_PROJECT_ENV = os.environ.get("PAPER_PROJECT")
PROJECT_ROOT = Path(_PROJECT_ENV).resolve() if _PROJECT_ENV else _REPO_ROOT
HAS_PROJECT = bool(_PROJECT_ENV)
if HAS_PROJECT:
    load_dotenv(PROJECT_ROOT / ".env", override=True)   # 项目级 .env 可覆盖模型配置

# ── 项目布局 ────────────────────────────────────────────────────────────
INBOX_ROOT = PROJECT_ROOT / "inbox"                       # 作者材料(只读)
WORK_ROOT = PROJECT_ROOT / "_work"                        # 中间产物
OUTPUT_ROOT = PROJECT_ROOT / "output"                     # 交付物
INDEX_DIR = WORK_ROOT / "index"                           # 程序生成的索引
WORKSPACE_ROOT = WORK_ROOT / "workspace"                  # 各章工作区

IDEA_PATH = INBOX_ROOT / "idea.md"                        # 贡献的唯一来源
DATA_ROOT = INBOX_ROOT / "results"                        # 实验结果(csv)+ 实验设置.md
REFERENCES_ROOT = INBOX_ROOT / "references"               # 文献室
REFS_BIB_PATH = REFERENCES_ROOT / "refs.bib"              # 引用 key 的唯一来源
CODE_ROOT = INBOX_ROOT / "code"                           # 实验代码(只读、不执行)
TEMPLATE_ROOT = INBOX_ROOT / "latex-template"             # 可选会议模板
SETTINGS_FILENAME = "实验设置.md"

# 程序生成的索引与目录:一律写在 _work/index/,不写进作者的 inbox。
DATA_INDEX_PATH = INDEX_DIR / "data-index.md"             # 数据导航索引
RES_IDS_PATH = INDEX_DIR / "res-ids.md"                   # RES 数字编号目录
REF_INDEX_PATH = INDEX_DIR / "ref-index.md"               # 文献总索引
CODE_INDEX_PATH = INDEX_DIR / "code-index.md"             # 代码静态索引

# Kept as a str for parity with older tool code.
PAPER_ROOT = str(WORKSPACE_ROOT)

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
# 重试归属（midstream plan v2 §5）：瞬时网络重试只由 ResilientModel 一层负责。
#   SDK max_retries      = MODEL_MAX_RETRIES（默认 0，底层不重试）
#   smolagents retryer   = OFF（ApiModel retry=False）
#   ResilientModel       = ON（唯一瞬时重试 owner，见 LLM_RETRY_*）
#   run_agent_stage      = 最后兜底（只对 TransientModelUnavailable 重跑，4 次纯兜底）
MODEL_TIMEOUT = os.getenv("MODEL_TIMEOUT")  # 留空 → 按 provider 默认（OpenAI 300 / Claude 600）
MODEL_MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "0"))

# ResilientModel 的瞬时重试计划：网关对长流式不稳，断了就在 Model 层快速重连，
# 而不是让整个 CodeAgent.run() 重跑几分钟。
LLM_RETRY_ENABLED = os.getenv("LLM_RETRY_ENABLED", "true").lower() == "true"
LLM_RETRY_MAX_ATTEMPTS = int(os.getenv("LLM_RETRY_MAX_ATTEMPTS", "3"))
LLM_RETRY_DELAY_1 = float(os.getenv("LLM_RETRY_DELAY_1", "0.8"))   # 第 1 次失败后等待（秒）
LLM_RETRY_DELAY_2 = float(os.getenv("LLM_RETRY_DELAY_2", "2.0"))   # 第 2 次失败后等待（秒）
LLM_RETRY_JITTER_RATIO = float(os.getenv("LLM_RETRY_JITTER_RATIO", "0.25"))
LLM_FAST_RETRY_AFTER_CAP = float(os.getenv("LLM_FAST_RETRY_AFTER_CAP", "15"))  # 服务端 Retry-After 上限

MAX_STEPS_AGENT = int(os.getenv("MAX_STEPS_AGENT", "15"))
MAX_STEPS_MANAGER = int(os.getenv("MAX_STEPS_MANAGER", "20"))

# ── Convergence loop (Stage 3) ──────────────────────────────────────────
# Keep revising until the first review's MUST FIX checklist is fully resolved,
# capped at MAX_REVISION_ROUNDS to avoid burning tokens on genuinely unfixable
# issues (missing data, etc.). The hard gate is "all MUST FIX resolved", not
# the score; REVIEW_SCORE_THRESHOLD only records the bar we report against.
MAX_REVISION_ROUNDS = int(os.getenv("MAX_REVISION_ROUNDS", "4"))
REVIEW_SCORE_THRESHOLD = float(os.getenv("REVIEW_SCORE_THRESHOLD", "4.0"))


def _model_timeout() -> float | None:
    """解析 MODEL_TIMEOUT：留空返回 None（由 router 按 provider 取默认）。"""
    if MODEL_TIMEOUT is None or MODEL_TIMEOUT == "":
        return None
    try:
        return float(MODEL_TIMEOUT)
    except (TypeError, ValueError):
        return None


def _retry_schedule(owner: str):
    """构造 ResilientModel 的重试计划。``LLM_RETRY_ENABLED=false`` 时返回 None
    （此时 build_model 不包 ResilientModel，行为退化到改造前，便于排障对照）。"""
    if not LLM_RETRY_ENABLED:
        return None
    from agents.retry_policy import RetrySchedule
    return RetrySchedule(
        max_attempts=LLM_RETRY_MAX_ATTEMPTS,
        delays=(LLM_RETRY_DELAY_1, LLM_RETRY_DELAY_2),
        jitter_ratio=LLM_RETRY_JITTER_RATIO,
        retry_after_cap=LLM_FAST_RETRY_AFTER_CAP,
    )


def get_draft_model():
    """Draft Agent — drafting and revision（最大推理深度）。"""
    from agents.model_router import build_model  # 懒加载：config 顶层不引入 smolagents

    return build_model(
        DRAFT_PROVIDER, DRAFT_MODEL, DRAFT_API_KEY, DRAFT_API_BASE,
        DRAFT_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
        retry_schedule=_retry_schedule("Draft"), retry_enabled=LLM_RETRY_ENABLED, owner="Draft",
    )


def get_review_model():
    """Review Agent — critique, verify, finalize（高推理深度）。"""
    from agents.model_router import build_model

    return build_model(
        REVIEW_PROVIDER, REVIEW_MODEL, REVIEW_API_KEY, REVIEW_API_BASE,
        REVIEW_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
        retry_schedule=_retry_schedule("Review"), retry_enabled=LLM_RETRY_ENABLED, owner="Review",
    )


def get_manager_model():
    """Manager Agent — Stage-1 planning and orchestration（高推理深度）。"""
    from agents.model_router import build_model

    return build_model(
        MANAGER_PROVIDER, MANAGER_MODEL, MANAGER_API_KEY, MANAGER_API_BASE,
        MANAGER_REASONING_LEVEL, timeout=_model_timeout(), max_retries=MODEL_MAX_RETRIES,
        retry_schedule=_retry_schedule("Manager"), retry_enabled=LLM_RETRY_ENABLED, owner="Manager",
    )
