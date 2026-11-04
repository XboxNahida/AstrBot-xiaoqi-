"""Preserve task-owned multimodal input without bloating protocol checkpoints."""

from __future__ import annotations

import copy
import hashlib

from astrbot.core.utils.media_utils import resolve_image_ref_to_base64_data


def capture_input(req, message_id, sanitize):
    """Copy the current input and recent history before chat tools mutate it.

    Args:
        req: Prepared host request with original text and attachments.
        message_id: Trusted source message identifier.
        sanitize: Host text sanitizer accepting a value and character bound.

    Returns:
        JSON-compatible current input and recent dialogue, without permissions.
    """
    current = []
    if req.prompt:
        current.append({"type": "text", "text": sanitize(req.prompt, 16000)})
    for part in getattr(req, "extra_user_content_parts", ()) or ():
        data = part if isinstance(part, dict) else part.model_dump()
        if data.get("type") == "text":
            current.append(
                {"type": "text", "text": sanitize(data.get("text", ""), 16000)}
            )
        elif data.get("type") == "image_url":
            current.append(copy.deepcopy(data))
    for url in getattr(req, "image_urls", ()) or ():
        part = {"type": "image_url", "image_url": {"url": str(url)}}
        if part not in current:
            current.append(part)
    contexts = []
    for item in (getattr(req, "contexts", ()) or ())[-12:]:
        if not isinstance(item, dict) or item.get("role") not in {
            "user",
            "assistant",
            "system",
        }:
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append(
                        {"type": "text", "text": sanitize(part.get("text", ""), 2000)}
                    )
                elif part.get("type") == "image_url":
                    parts.append(copy.deepcopy(part))
            content = parts
        else:
            content = sanitize(content, 2000)
        contexts.append({"role": item["role"], "content": content})
    return {
        "current_input": {"message_id": str(message_id), "content": current},
        "contexts": contexts,
    }


async def freeze_images(request):
    """Persist image bytes inside the task metadata, indexed by short references.

    Args:
        request: Task request modified in place before durable submission.

    Raises:
        ValueError: A current attachment cannot be read. Never substitute history.
    """
    media = request.setdefault("media", {})
    resolved = {}
    messages = [*request.get("contexts", ()), request.get("current_input", {})]
    for index, item in enumerate(messages):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") != "image_url":
                continue
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("pc-task-image://") and url in media:
                continue
            try:
                if url not in resolved:
                    data = await resolve_image_ref_to_base64_data(url, strict=True)
                    if data is None:
                        raise ValueError("No image data")
                    value = data.to_data_url()
                    key = (
                        "pc-task-image://" + hashlib.sha256(value.encode()).hexdigest()
                    )
                    media[key] = value
                    resolved[url] = key
                part["image_url"]["url"] = resolved[url]
            except Exception:
                if index == len(messages) - 1:
                    raise ValueError("image_input_unavailable") from None
                part.clear()
                part.update(
                    type="text",
                    text="[历史图片已不可用；不要推测画面或将其当成本轮附件。]",
                )


def materialize_images(contexts, request, *, supports_image=True):
    """Expand task-owned image references only for the actual provider call.

    Args:
        contexts: Bounded protocol messages, possibly restored from a checkpoint.
        request: Same task's durable media map.
        supports_image: Whether the selected provider accepts native image parts.

    Returns:
        Independent protocol messages containing native data URL image parts.

    Raises:
        ValueError: A persisted image reference has no corresponding task bytes.
    """
    result = copy.deepcopy(contexts)
    for message in result:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") != "image_url":
                continue
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("pc-task-image://"):
                saved = request.get("media", {}).get(url)
                if not saved:
                    raise ValueError("image_input_unavailable")
                part["image_url"]["url"] = saved
            if not supports_image:
                part.clear()
                part.update(
                    type="text",
                    text="[此处有来源绑定的图片，当前模型不支持直接读取。需要画面事实时调用pc_inspect_images；不要猜测或拿旧题替代。]",
                )
    return result
