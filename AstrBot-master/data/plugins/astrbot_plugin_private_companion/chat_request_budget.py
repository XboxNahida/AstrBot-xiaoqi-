"""聊天预算接线，保留固定前缀、完整授权和请求局部工具绑定。"""

from copy import copy

from astrbot.core.agent.context.budget import ChatInputBudget
from astrbot.core.agent.tool import ToolSet

from .chat_input_compaction import _compact_generated_skills


def prepare_chat_budget(req, fixed_fragments=()):
    """为已登记聊天启用软预算，只移动有宿主来源证据的固定片段。

    Args:
        req: 已通过身份核验的聊天请求。
        fixed_fragments: 本轮宿主追加的固定说明原文，不从人格猜测提取。
    """
    if getattr(req, "_chat_input_budget", None) is not None:
        return
    base = getattr(req, "_companion_budget_base", None)
    if isinstance(base, str):
        base = _compact_generated_skills(base)
    if isinstance(base, str) and base and req.system_prompt.startswith(base):
        tail = req.system_prompt[len(base) :]
        fixed = []
        for fragment in fixed_fragments:
            if fragment and fragment in tail:
                tail = tail.replace(fragment, "", 1)
                fixed.append(fragment)
        req.system_prompt = base + "".join(fixed) + tail
    budget = ChatInputBudget(req)
    req._chat_input_budget = budget
    tools = ToolSet(
        tools=[copy(tool) for tool in (req.func_tool.tools if req.func_tool else [])]
    )
    if tools.get_tool("pc_read_tool_result") is None:
        tools.add_tool(budget.read_tool())
    tools.tools.sort(key=lambda tool: tool.name)
    req.func_tool = tools
