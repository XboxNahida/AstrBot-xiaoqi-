"""Bound hub inputs and coalesce ambient decisions without merging replies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from typing import Any


def compact_hub_payload(payload: dict, limit: int = 6) -> dict:
    """Copy and bound observation data while preserving current requests.

    Args:
        payload: Full hub facts, never modified in place.
        limit: Maximum recent messages for ordinary human conversations.

    Returns:
        Reduced facts with task protection and identity fields preserved.
    """
    result = deepcopy(payload)
    scene = result.get("platform_scene") or {}
    # Bot collaboration requires the original human evidence, not a summary.
    bot_sender = (scene.get("sender_identity") or {}).get("kind") == "bot"
    if not bot_sender:
        result["recent_messages"] = result.get("recent_messages", [])[
            -max(2, min(12, limit)) :
        ]
        for row in result["recent_messages"]:
            row["text"] = str(row.get("text") or "")[:320]
            row["image_description"] = str(row.get("image_description") or "")[:240]
    participation = result.get("group_participation") or {}
    if participation:
        participation["recent_bot_replies"] = participation.get(
            "recent_bot_replies", []
        )[-3:]
        for row in participation["recent_bot_replies"]:
            row["text"] = str(row.get("text") or "")[:240]
        # pending_requests already supplies these facts with stable event IDs.
        participation.pop("pending_replies", None)
    for row in scene.get("pending_requests", []):
        if not row.get("protected") and not row.get("send_started"):
            row["text"] = str(row.get("text") or "")[:240]
    return result


def summarize_hub_persona(text: str) -> str:
    """Extract a small decision-oriented persona view without an LLM call.

    Args:
        text: Original selected persona, retained by the reply model.

    Returns:
        At most 500 characters of identity, emotion and boundary excerpts.
    """
    text = str(text or "").strip()
    if len(text) <= 500:
        return text
    sentences = [s.strip() for s in re.split(r"\n+|(?<=[。！？；])", text) if s.strip()]
    keywords = (
        "身份",
        "你是",
        "名字",
        "性格",
        "情绪",
        "嘴硬",
        "破防",
        "找补",
        "温柔",
        "生气",
        "挑逗",
        "群聊",
        "接话",
        "边界",
        "禁止",
        "不要",
        "不能",
        "称呼",
    )
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (
            -(sum(word in item[1] for word in keywords) + (3 if item[0] == 0 else 0)),
            item[0],
        ),
    )
    selected = []
    size = 0
    for index, sentence in ranked:
        if len(sentence) > 499:
            continue
        if size + len(sentence) + bool(selected) <= 500:
            selected.append((index, sentence))
            size += len(sentence) + bool(size)
    return "\n".join(sentence for _, sentence in sorted(selected)) or text[:500]


def hub_semantic_stamp(payload: dict) -> str:
    """Hash semantic facts without incidental passage-of-time fields.

    Args:
        payload: Full decision inputs, including protected queue and identity.

    Returns:
        Stable digest for reuse within the same event only.
    """
    volatile = {
        "current_time",
        "gap_since_previous_seconds",
        "waiting_seconds",
        "elapsed_seconds",
        "seconds_since_last_bot_reply",
    }

    def stable(value):
        if isinstance(value, dict):
            return {
                key: stable(item) for key, item in value.items() if key not in volatile
            }
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            stable(payload), ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()


async def call_hub_model(
    plugin: Any,
    *,
    context: Any,
    scene: dict,
    payload: dict,
    prompt: str,
    provider_id: str,
    max_tokens: int,
    persona_revision: str = "",
    hub_request_attempts: dict[str, int] | None = None,
) -> str | None:
    """Batch ambient judgments while returning an independent result per event.

    Args:
        plugin: Runtime owner exposing the existing model call.
        context: Original event context.
        scene: Platform facts including stable message identity.
        payload: Original facts for this event, after optional compaction.
        prompt: Existing complete decision instructions.
        provider_id: Selected hub provider.
        max_tokens: Original per-event output limit.
        persona_revision: Full persona digest, preventing mixed revisions.

    Returns:
        Original per-event JSON, or None for a missing or invalid batch result.
    """
    attempts = hub_request_attempts if hub_request_attempts is not None else {}
    identity = str(scene.get("message_id") or "")
    explicit = scene.get("trigger") in {
        "at_bot",
        "reply_bot",
        "group_wakeup_name",
        "group_wakeup_direct_word",
        "at_all",
    }
    controls = re.search(
        r"取消|停止|重试|继续|暂停|进度|结果|任务|帮我|帮忙|修改|改成|设置|启动|关闭|执行|编程|写.{0,5}(代码|程序|脚本)",
        context.current_text,
    )
    sender_kind = (scene.get("sender_identity") or {}).get("kind", "unknown")
    batchable = (
        bool(getattr(plugin, "enable_hub_group_batching", False))
        and context.conversation_type == "group"
        and identity
        and not explicit
        and not controls
        and sender_kind not in {"bot", "possible_bot"}
        and not payload.get("background_task")
        and not scene.get("pending_requests")
        and not context.has_current_image
        and not context.has_current_voice
    )
    if not batchable:
        return await plugin._llm_call(
            prompt,
            max_tokens=max_tokens,
            provider_id=provider_id,
            task="astrbot_reply",
            timeout_key="CONVERSATION_HUB_PROVIDER_ID",
            hub_request_attempts=attempts,
        )
    batches = getattr(plugin, "_hub_group_batches", None)
    if not isinstance(batches, dict):
        batches = {}
        plugin._hub_group_batches = batches
    key = (
        context.task_session_key or context.scope,
        context.scope,
        context.conversation_id,
        provider_id,
        persona_revision,
        json.dumps(payload.get("bot_identity", {}), sort_keys=True),
    )
    batch = batches.get(key)
    if batch is None or len(batch["items"]) >= 6:
        batch = {"items": []}
        batches[key] = batch

        async def flush():
            try:
                # A fixed deadline, never extended by a new arrival.
                wait = float(getattr(plugin, "hub_group_batch_seconds", 2.5))
                await asyncio.sleep(max(0.0, min(5.0, wait)))
                if batches.get(key) is batch:
                    batches.pop(key, None)
                items = [item for item in batch["items"] if not item["future"].done()]
                if not items:
                    return
                if len(items) == 1:
                    item = items[0]
                    raw = await plugin._llm_call(
                        item["prompt"],
                        max_tokens=max_tokens,
                        provider_id=provider_id,
                        task="astrbot_reply",
                        timeout_key="CONVERSATION_HUB_PROVIDER_ID",
                        hub_request_attempts=item["attempts"],
                    )
                    if not item["future"].done():
                        item["future"].set_result(raw)
                    return
                # Latest full context contains the shared scene. Every event
                # retains its own current sender and complete message below.
                latest = items[-1]
                entries = [
                    {"message_id": item["identity"], "facts": item["payload"]}
                    for item in items
                ]
                for entry in entries:
                    for field in ("recent_messages", "group_participation"):
                        entry["facts"].pop(field, None)
                shared_prompt = latest["prompt"]
                batch_prompt = shared_prompt + (
                    "\n【本次为群聊批量判断】共享背景与规则沿用上文。以下各条是不同的真实入站消息，不是当前发言者说的同一句话。"
                    "分别判断每条是否需要回复，不合并发言者身份，不把其他成员的话归给当前成员。"
                    "群友连续附和、重复要点或同一问题的补充优先只授权一个合适的事件，独立的新问题分别判断。"
                    '本次只输出一个JSON对象，格式优先为：{"decisions":[{"message_id":"原消息ID",'
                    '"reply":false,"actions":[],"reason":"原因"}]}。'
                    "每项沿用上文的全部路由/情绪/工具字段；每个消息ID恰好一次。每条的身份和platform_scene以该条facts为准。\n"
                    + json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
                )
                shared_attempts = {
                    provider: max(item["attempts"].get(provider, 0) for item in items)
                    for item in items for provider in item["attempts"]
                }
                raw = await plugin._llm_call(
                    batch_prompt,
                    max_tokens=min(2400, max_tokens * len(items)),
                    provider_id=provider_id,
                    task="astrbot_reply",
                    timeout_key="CONVERSATION_HUB_PROVIDER_ID",
                    hub_request_attempts=shared_attempts,
                )
                for item in items:
                    item["attempts"].update(shared_attempts)
                try:
                    clean = re.sub(
                        r"^```(?:json)?\s*|\s*```$",
                        "",
                        str(raw or "").strip(),
                        flags=re.I,
                    )
                    rows = json.loads(clean).get("decisions", [])
                    results = {}
                    duplicates = set()
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(
                            row.get("message_id"), str
                        ):
                            continue
                        message_id = row["message_id"]
                        if message_id in results:
                            duplicates.add(message_id)
                        results[message_id] = row
                    for item in items:
                        row = results.get(item["identity"])
                        valid = row is not None and item["identity"] not in duplicates
                        if not item["future"].done():
                            item["future"].set_result(
                                json.dumps(row, ensure_ascii=False) if valid else None
                            )
                except (ValueError, TypeError, AttributeError):
                    for item in items:
                        if not item["future"].done():
                            item["future"].set_result(None)
            finally:
                if batches.get(key) is batch:
                    batches.pop(key, None)
                for item in batch["items"]:
                    if not item["future"].done():
                        item["future"].set_result(None)

        creator = getattr(plugin, "_create_lifecycle_background_task", None)
        batch["task"] = (
            creator(flush(), label="hub_group_batch")
            if callable(creator)
            else asyncio.create_task(flush())
        )
        if batch["task"] is None:
            if batches.get(key) is batch:
                batches.pop(key, None)
            return None

        def release_waiters(task):
            # Cancellation before flush starts does not enter its finally block.
            if batches.get(key) is batch:
                batches.pop(key, None)
            for item in batch["items"]:
                if not item["future"].done():
                    item["future"].set_result(None)
            if not task.cancelled():
                task.exception()

        batch["task"].add_done_callback(release_waiters)
    future = asyncio.get_running_loop().create_future()
    batch["items"].append(
        {
            "identity": identity,
            "payload": deepcopy(payload),
            "prompt": prompt,
            "future": future,
            "attempts": attempts,
        }
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        future.cancel()
        raise
