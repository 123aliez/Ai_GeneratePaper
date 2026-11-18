"""日志标签:当前正在说话的角色。

模型不再有任何工具(读文件、写文件、查文献都由脚本在 content_stage / materials 里完成),
这里只剩终端日志用的角色标签。
"""

_current_agent = "Agent"

AGENT_LABELS = {"Draft": "起草", "Review": "评审", "Manager": "管理", "Agent": "Agent"}


def set_agent_context(name: str) -> None:
    global _current_agent
    _current_agent = name


def agent_label(name: str) -> str:
    return AGENT_LABELS.get(name.strip(), name.strip())
