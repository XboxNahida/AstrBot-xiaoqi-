"""Expose optional visual analysis of images already bound to this interaction."""

from __future__ import annotations

import json

import jsonschema

from astrbot.core.agent.tool import FunctionTool

from .task_multimodal import materialize_images


class ImageInspectionTool(FunctionTool):
    """Read current or recent images without accepting paths or identity arguments.

    Args:
        runtime: Host runtime with configured vision providers and trusted events.
        event: Original frontend event, or None for the host-bound task context.
    """

    def __init__(self, runtime, event=None):
        super().__init__(
            name="pc_inspect_images",
            description=(
                "按需调用独立识图主备模型，细读本轮图片、公式、表情包或核对视觉细节。"
                "常规图文已经直接交给你，无需每次调用。source=current读取本轮附件，recent读取最近一条含图用户历史。"
                "history配合pc_read_chat_history返回的image_refs读取指定旧图。"
                "只能读取当前会话/任务已绑定的图片，不接受文件路径、他人账号或任务ID。"
                "image_indices从0开始；省略则读取该来源全部图片。返回视觉事实，不代表后台任务已完成。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "source": {"type": "string", "enum": ["current", "recent", "history"]},
                    "image_refs": {
                        "type": "array", "items": {"type": "string", "maxLength": 64},
                        "minItems": 1, "maxItems": 5, "uniqueItems": True,
                        "description": "source=history时使用pc_read_chat_history返回的image_ref，不能传路径或URL。",
                    },
                    "image_indices": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "uniqueItems": True,
                        "minItems": 1,
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event) if event is not None else None

    async def call(self, context, **kwargs):
        """Use configured visual analysis only after a model explicitly calls it.

        Args:
            context: Host-created execution context, never model-supplied identity.
            **kwargs: Specific visual question and optional source/index selection.

        Returns:
            JSON factual result or a specific, repairable failure receipt.
        """
        try:
            jsonschema.validate(kwargs, self.parameters)
        except jsonschema.ValidationError:
            return json.dumps({"success": False, "code": "invalid_arguments"})
        rt = self.runtime
        event = self.event or getattr(getattr(context, "context", None), "event", None)
        if event is None:
            return json.dumps({"success": False, "code": "untrusted_event"})
        if self.event is not None:
            capture = rt._events.get(event)
            if (
                not capture
                or not rt._trusted_event(event)
                or rt.scope(event) != self.scope
            ):
                return json.dumps({"success": False, "code": "untrusted_event"})
            request = capture["request"]
        else:
            task_id = getattr(event, "_private_companion_source_task_id", "")
            job = rt.service.get(task_id, rt.scope(event)) if task_id else None
            if job is None or not rt.authorize_job(job):
                return json.dumps({"success": False, "code": "untrusted_event"})
            request = job.metadata.get("request", {})
        source = kwargs.get("source", "current")
        if source == "history" and self.event is None:
            return json.dumps({"success": False, "code": "history_requires_bound_chat"})
        if source != "history" and kwargs.get("image_refs"):
            return json.dumps({"success": False, "code": "invalid_arguments"})
        candidates = (
            [request.get("current_input", {})]
            if source == "current"
            else [
                item
                for item in reversed(request.get("contexts", ()))
                if item.get("role") == "user"
            ]
        )
        images = []
        try:
            history_map = rt._events[event].get("history_images", {}) if self.event is not None else {}
            if source == "history":
                refs = kwargs.get("image_refs", [])
                if not refs or any(ref not in history_map for ref in refs):
                    return json.dumps({"success": False, "code": "history_image_not_in_scope"})
                images = [history_map[ref] for ref in refs]
            for item in (() if source == "history" else materialize_images(candidates, request)):
                parts = item.get("content", ())
                if isinstance(parts, list):
                    images = [
                        part["image_url"]["url"]
                        for part in parts
                        if part.get("type") == "image_url"
                    ]
                if images:
                    break
            indices = kwargs.get("image_indices", list(range(len(images))))
            selected = [images[index] for index in indices]
        except (ValueError, IndexError, KeyError):
            return json.dumps({"success": False, "code": "image_input_unavailable"})
        if not selected:
            return json.dumps(
                {"success": False, "code": "no_bound_images", "source": source}
            )
        transcribe = getattr(rt.plugin, "_transcribe_private_inbound_images", None)
        if not callable(transcribe):
            return json.dumps({"success": False, "code": "vision_provider_unavailable"})
        try:
            text = await transcribe(
                selected,
                umo=event.unified_msg_origin,
                user_text=kwargs["question"],
                force_contextual=True,
            )
        except Exception:
            return json.dumps({"success": False, "code": "vision_request_failed"})
        return json.dumps(
            {
                "success": bool(text),
                "code": "observed" if text else "vision_provider_unavailable",
                "source": source,
                "image_count": len(selected),
                "text": rt._safe_text(text, 16000),
            },
            ensure_ascii=False,
        )
