"""三个角色(Manager / Draft / Review)= 模型 + 系统提示词。

角色没有工具、没有步数循环:读什么、写什么、校验和重试都由脚本在 content_stage 里完成,
模型只负责产出内容(见 content_stage.py 的说明)。
"""

from smolagents import Model

from .content_stage import ContentAgent
from .prompts import DRAFT_INSTRUCTIONS, REVIEW_INSTRUCTIONS, MANAGER_INSTRUCTIONS


def create_planner_agent(model_manager: Model) -> ContentAgent:
    """只装一个 Manager —— 给 `--expand` 这类纯规划步骤用。

    不复用 create_agents 是因为那会连带初始化 Draft/Review 两个模型:展开 outline
    根本不调它们,而三组 key 里任意一组没配好就会在这一步白报错。
    """
    return ContentAgent(model_manager, MANAGER_INSTRUCTIONS, "Manager")


def create_agents(model_draft: Model, model_review: Model, model_manager: Model):
    """返回 (manager, draft_agent, review_agent)。"""
    draft_agent = ContentAgent(model_draft, DRAFT_INSTRUCTIONS, "Draft")
    review_agent = ContentAgent(model_review, REVIEW_INSTRUCTIONS, "Review")
    manager_agent = ContentAgent(model_manager, MANAGER_INSTRUCTIONS, "Manager")
    return manager_agent, draft_agent, review_agent
