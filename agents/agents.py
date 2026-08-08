from smolagents import CodeAgent, LiteLLMModel

from .tools import read_file, write_file, list_folder, search_references, search_literature
from .prompts import DRAFT_INSTRUCTIONS, REVIEW_INSTRUCTIONS, MANAGER_INSTRUCTIONS

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import MAX_STEPS_AGENT, MAX_STEPS_MANAGER

AUTHORIZED_IMPORTS = ["os", "json", "time", "textwrap"]


def create_planner_agent(model_manager: LiteLLMModel):
    """只装一个 Manager,不带 managed_agents —— 给 `--expand` 这类纯规划步骤用。

    不复用 create_agents 是因为那会连带初始化 Draft/Review 两个模型:展开 outline
    根本不调它们,而三组 key 里任意一组没配好就会在这一步白报错。
    """
    return CodeAgent(
        tools=[read_file, write_file, list_folder],
        model=model_manager,
        instructions=MANAGER_INSTRUCTIONS,
        additional_authorized_imports=AUTHORIZED_IMPORTS,
        max_steps=MAX_STEPS_MANAGER,
        verbosity_level=0,
        stream_outputs=True,
    )


def create_agents(
    model_draft: LiteLLMModel,
    model_review: LiteLLMModel,
    model_manager: LiteLLMModel,
):
    """Create the 3-agent hierarchy: Manager -> (Draft, Review)."""

    draft_agent = CodeAgent(
        tools=[read_file, write_file, list_folder, search_references, search_literature],
        model=model_draft,
        name="draft_agent",
        description=(
            "Drafts paper sections from brief and input materials. "
            "Input: the folder path containing brief.md and input.md."
        ),
        instructions=DRAFT_INSTRUCTIONS,
        additional_authorized_imports=AUTHORIZED_IMPORTS,
        max_steps=MAX_STEPS_AGENT,
        verbosity_level=0,
        stream_outputs=True,
    )

    review_agent = CodeAgent(
        tools=[read_file, write_file, list_folder, search_references, search_literature],
        model=model_review,
        name="review_agent",
        description=(
            "Reviews paper drafts and produces detailed feedback. "
            "For finalization, merges all versions into final.md. "
            "Input: the folder path containing the draft."
        ),
        instructions=REVIEW_INSTRUCTIONS,
        additional_authorized_imports=AUTHORIZED_IMPORTS,
        max_steps=MAX_STEPS_AGENT,
        verbosity_level=0,
        stream_outputs=True,
    )

    manager_agent = CodeAgent(
        tools=[read_file, write_file, list_folder, search_references, search_literature],
        model=model_manager,
        managed_agents=[draft_agent, review_agent],
        instructions=MANAGER_INSTRUCTIONS,
        additional_authorized_imports=AUTHORIZED_IMPORTS,
        max_steps=MAX_STEPS_MANAGER,
        verbosity_level=0,
        stream_outputs=True,
    )

    return manager_agent, draft_agent, review_agent
