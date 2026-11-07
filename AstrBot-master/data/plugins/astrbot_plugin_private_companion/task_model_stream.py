"""后台任务复用核心流式超时策略，重试由持久任务服务统一管理。"""

from astrbot.core.provider.streaming import (
    ModelStreamTimeout as TaskModelStreamTimeout,
)
from astrbot.core.provider.streaming import (
    collect_model_response,
)

__all__ = ["TaskModelStreamTimeout", "collect_task_model_response"]


async def collect_task_model_response(provider, **kwargs):
    kwargs.setdefault("timeout", 60)
    kwargs["request_max_retries"] = 1
    return await collect_model_response(provider, **kwargs)
