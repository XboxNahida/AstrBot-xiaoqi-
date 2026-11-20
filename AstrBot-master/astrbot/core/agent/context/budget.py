"""聊天请求软预算：仅投影可恢复工具文本，不裁剪用户材料。"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache

import jsonschema

from ..tool import FunctionTool


@lru_cache(maxsize=1)
def _encoder():
    """返回已安装的近似分词器；不可用时使用字符估算。

    Returns:
        分词器或None，不代表供应商的精确计费口径。
    """
    try:
        from tiktoken.registry import ENCODINGS

        # get_encoding可能在冷启动时联网下载且无超时；热路径仅读已加载编码。
        return ENCODINGS.get("o200k_base")
    except Exception:
        return None


class ChatInputBudget:
    """管理一个聊天请求的预算和可恢复工具材料。

    Args:
        request: 宿主绑定的独立会话请求，不共享跨用户正文。
    """

    target = 20000
    organize_at = 25000
    tool_pressure_at = 30000
    observe_at = 40000

    def __init__(self, request):
        self.request = request
        self.previews = {}
        self.requests = 0
        if not hasattr(request, "history_lookup_archive"):
            request.history_lookup_archive = {}

    def count(self, value):
        """估算文本或JSON的Token数。

        Args:
            value: 不含媒体编码的文本或结构。

        Returns:
            非供应商计费口径的近似Token数。
        """
        text = (
            value
            if isinstance(value, str)
            else json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            )
        )
        encoder = _encoder()
        if encoder is not None:
            return len(encoder.encode(text, disallowed_special=()))
        return sum(1 if ord(ch) > 127 else 0.35 for ch in text).__ceil__()

    def measure(self, messages, tools):
        """分项统计消息和完整工具定义，媒体只做估算。

        Args:
            messages: 实际提供商协议消息。
            tools: 当前实际可用工具集合。

        Returns:
            只含数字分项的字典。
        """
        stats = {
            "system": 0,
            "user": 0,
            "assistant": 0,
            "tool": 0,
            "calls": 0,
            "media": 0,
        }
        for message in messages:
            if hasattr(message, "model_dump"):
                message = message.model_dump()
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            content = message.get("content")
            if isinstance(content, str):
                stats[role] += self.count(content)
            elif isinstance(content, list):
                for part in content:
                    if hasattr(part, "model_dump"):
                        part = part.model_dump()
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type", "")
                    if kind in {"image_url", "image", "input_image"}:
                        stats["media"] += 765
                    elif kind in {"audio_url", "input_audio"}:
                        stats["media"] += 500
                    else:
                        stats[role] += self.count(part)
            if message.get("tool_calls"):
                stats["calls"] += self.count(message["tool_calls"])
        stats["tools"] = self.count(tools.openai_schema()) if tools else 0
        stats["total"] = sum(stats.values()) + 4 * len(messages)
        return stats

    def project(self, messages, tools):
        """只对超预算的大工具正文做可逆预览，同轮已投影内容保持稳定。

        Args:
            messages: 原始消息，函数不会就地修改。
            tools: 完整工具定义。

        Returns:
            请求局部消息及分项统计；必要内容可超过软预算。
        """
        result = [
            message.model_dump()
            if hasattr(message, "model_dump")
            else copy.deepcopy(message)
            for message in messages
        ]
        before = self.measure(result, tools)["total"]
        reader = tools.get_tool("pc_read_tool_result") if tools else None
        if reader is None or not reader.active:
            stats = self.measure(result, tools)
            stats.update(
                before=before,
                projected_results=0,
                over_target=before > self.target,
                over_observe=before > self.observe_at,
            )
            return copy.deepcopy(messages), stats
        calls = {}
        for message in result:
            for call in message.get("tool_calls") or []:
                calls[call.get("id")] = call.get("function", {}).get("name")
        for message in result:
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") in self.previews
            ):
                message["content"] = self.previews[message["tool_call_id"]]
        total = self.measure(result, tools)["total"]
        threshold = self.organize_at if self.requests == 0 else self.tool_pressure_at
        for message in result:
            if total <= threshold:
                break
            key, body = message.get("tool_call_id"), message.get("content")
            if (
                message.get("role") != "tool"
                or not key
                or key in self.previews
                or key not in calls
                or calls[key] == "pc_read_tool_result"
                or not isinstance(body, str)
                or self.count(body) <= 4000
            ):
                continue
            # 错误证据不做预览，防止修复原因消失；不推断模型已消费哪些事实。
            if re.search(
                r'(?i)traceback|\berror\b|\bfailed\b|"ok"\s*:\s*false|错误|失败|异常',
                body,
            ):
                continue
            try:
                envelope = json.loads(body)
            except (ValueError, TypeError):
                envelope = None
            if isinstance(envelope, dict) and any(
                str(envelope.get(field, "")).lower()
                in {
                    "timeout",
                    "timed_out",
                    "cancelled",
                    "canceled",
                    "permission_denied",
                    "unknown_result",
                    "unknown",
                    "interrupted",
                    "expired",
                    "denied",
                }
                for field in ("status", "outcome", "code")
            ):
                continue
            original = self.request.history_lookup_archive.get(key)
            if original is not None and original != body:
                continue
            self.request.history_lookup_archive[key] = body
            width = min(len(body) // 3, 6000)
            while width > 64 and self.count(body[:width] + body[-width:]) > 2600:
                width = width * 3 // 4
            preview = (
                body[:width]
                + "\n【工具正文预览：中间部分未加载，不是完整结果或模型总结】\n"
                + body[-width:]
                + "\n完整正文已保留；需要细节时调用pc_read_tool_result，参数"
                + json.dumps(
                    {"tool_call_id": key, "offset": 0, "limit": 6000},
                    ensure_ascii=False,
                )
                + f"；全文{len(body)}字符，offset/next_offset均为字符位置。"
            )
            self.previews[key] = preview
            message["content"] = preview
            total -= self.count(body) - self.count(preview)
        self.requests += 1
        stats = self.measure(result, tools)
        stats.update(
            before=before,
            projected_results=len(self.previews),
            over_target=stats["total"] > self.target,
            over_observe=stats["total"] > self.observe_at,
        )
        for index, original in enumerate(messages):
            if not hasattr(original, "model_copy"):
                continue
            projected = result[index]
            result[index] = original.model_copy(deep=True)
            if (
                original.role == "tool"
                and isinstance(original.content, str)
                and isinstance(projected.get("content"), str)
            ):
                result[index].content = projected["content"]
        return result, stats

    def read_tool(self):
        """构造仅读当前请求工具材料的分页接口。

        Returns:
            不接受文件路径、其他会话ID或执行命令的工具。
        """
        budget = self

        class ReadTool(FunctionTool):
            async def call(self, context, **kwargs):
                try:
                    jsonschema.validate(kwargs, self.parameters)
                except jsonschema.ValidationError:
                    return json.dumps({"ok": False, "code": "invalid_arguments"})
                key = kwargs["tool_call_id"]
                text = budget.request.history_lookup_archive.get(key)
                if not isinstance(text, str):
                    return json.dumps(
                        {"ok": False, "code": "result_not_in_current_request"}
                    )
                offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 6000)
                end = min(len(text), offset + limit)
                return json.dumps(
                    {
                        "ok": True,
                        "tool_call_id": key,
                        "offset": offset,
                        "total_characters": len(text),
                        "text": text[offset:end],
                        "next_offset": end if end < len(text) else None,
                    },
                    ensure_ascii=False,
                )

        return ReadTool(
            name="pc_read_tool_result",
            description="按tool_call_id分页读取本轮工具预览的完整原文。仅已有材料，无执行副作用；offset/next_offset是字符位置，读到所需细节即可。",
            parameters={
                "type": "object",
                "properties": {
                    "tool_call_id": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 80, "maximum": 32000},
                },
                "required": ["tool_call_id"],
                "additionalProperties": False,
            },
        )
