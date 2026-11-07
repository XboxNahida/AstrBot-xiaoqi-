"""后台任务的独立模型和单次请求预算，不修改共享提供器。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskModelPolicy:
    provider_id: str
    timeout_seconds: float = 60
    max_attempts: int = 3

    def as_request(self) -> dict:
        return {
            "primary": self.provider_id,
            "fallback": "",
            "request_timeout_seconds": self.timeout_seconds,
            "request_max_attempts": self.max_attempts,
        }


def request_policy(request: Mapping) -> TaskModelPolicy:
    """max_attempts包含首次请求；旧备用字段不参与任务执行。"""
    provider = request.get("primary", "")
    timeout = request.get("request_timeout_seconds", 60)
    attempts = request.get("request_max_attempts", 3)
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("需要有效的任务模型ID")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or timeout <= 0):
        raise ValueError("单次请求超时必须是大于0的有限秒数")
    if type(attempts) is not int or attempts < 1:
        raise ValueError("最多请求次数必须是至少1次的整数，包含首次请求")
    return TaskModelPolicy(provider.strip(), min(float(timeout), 60), attempts)
