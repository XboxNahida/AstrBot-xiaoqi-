"""Estimate native task windows and identify complete, replaceable tool rounds."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping

from astrbot.core.utils.llm_metadata import LLM_METADATAS


def context_budget(provider, fallback=128000):
    """Read explicit window metadata without guessing from a model name.

    Args:
        provider: The task's selected provider instance.
        fallback: Host-scoped webpage provider setting for unknown models.

    Returns:
        Configured token window, or a conservative unknown-model fallback.
    """
    config = getattr(provider, "provider_config", {})
    sources = [config]
    if isinstance(config, Mapping):
        sources.append(config.get("model_metadata", {}))
    sources.append(getattr(provider, "model_metadata", {}))
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("max_context_tokens", "context_window", "context_length"):
            value = source.get(key)
            if type(value) is int and value > 0:
                return value
    get_model = getattr(provider, "get_model", None)
    model = get_model() if callable(get_model) else config.get("model", "")
    metadata = LLM_METADATAS.get(model, {})
    value = metadata.get("limit", {}).get("context", 0)
    if type(value) is int and value > 0:
        return value
    value = config.get("fallback_max_context_tokens", fallback)
    return value if type(value) is int and value > 0 else 128000


def estimate_tokens(contexts, system_prompt, tools, output_reserve=2048):
    """Estimate all request text, tool schemas, images and completion allowance.

    Args:
        contexts: Protocol messages, including materialized images.
        system_prompt: Complete system instructions.
        tools: JSON tool schemas.
        output_reserve: Minimum output token allowance.

    Returns:
        Conservative UTF-8 byte-weighted token estimate, not a tokenizer or
        provider billing measurement. Image data bytes are estimated separately.
    """
    text_contexts = copy.deepcopy(contexts)
    for message in text_contexts:
        message.pop("task_context_summary", None)
        for part in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else []
        ):
            if isinstance(part, dict) and part.get("type") in {
                "image_url",
                "image",
                "input_image",
            }:
                part.clear()
                part.update(type="image_url", image_url={"url": "[image]"})
    payload = json.dumps([system_prompt, text_contexts, tools], ensure_ascii=False)
    images = sum(
        1
        for message in contexts
        for item in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
        if isinstance(item, dict)
        and item.get("type") in {"image_url", "image", "input_image"}
    )
    return (
        math.ceil(len(payload.encode("utf-8")) * 0.6)
        + images * 4096
        + len(contexts) * 8
        + output_reserve
    )


def summary_slice(contexts):
    """Locate old closed tool rounds while retaining two recent raw rounds.

    Args:
        contexts: Task-local messages. Unclosed calls are never summarized.

    Returns:
        A replaceable (start, end) range or None. User inputs stay verbatim.
    """
    rounds = []
    index = 0
    while index < len(contexts):
        item = contexts[index]
        calls = item.get("tool_calls") if item.get("role") == "assistant" else None
        if not calls:
            index += 1
            continue
        ids = {call.get("id") for call in calls}
        end = index + 1
        seen = set()
        while end < len(contexts) and contexts[end].get("role") == "tool":
            seen.add(contexts[end].get("tool_call_id"))
            end += 1
        if ids == seen and None not in ids:
            rounds.append((index, end))
        index = end
    if len(rounds) <= 2:
        return None
    start, end = rounds[0]
    # Stop at corrections or incomplete batches, leaving them verbatim.
    for following_start, following_end in rounds[1:-2]:
        if following_start != end:
            break
        end = following_end
    if start and contexts[start - 1].get("task_context_summary") is True:
        start -= 1
    return start, end
