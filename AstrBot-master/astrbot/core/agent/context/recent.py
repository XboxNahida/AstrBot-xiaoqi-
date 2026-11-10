"""Keep a recent request view separate from its complete conversation archive."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid


def cache_namespace(kind, *identity):
    """Return a stable opaque key for one host-bound conversation.

    Args:
        kind: Chat or task namespace, never a user-selected scope.
        *identity: Host-resolved identity components.

    Returns:
        A short key without raw account or conversation identifiers.
    """
    value = json.dumps([kind, *map(str, identity)], ensure_ascii=False)
    return "astrbot-" + kind + "-" + hashlib.sha256(value.encode()).hexdigest()[:40]


def select_recent(contexts, turns):
    """Select complete recent user turns without modifying stored history.

    Args:
        contexts: Original protocol dictionaries, including tool call pairs.
        turns: Maximum user turns; negative values retain all original turns.

    Returns:
        Independent messages with old generated summary pairs excluded.
    """
    messages = []
    skip_ack = False
    for item in contexts:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if isinstance(content, str) and content.startswith(
            "Our previous history conversation summary: "
        ) and item.get("role") == "user":
            skip_ack = True
            continue
        if skip_ack:
            skip_ack = False
            if item.get("role") == "assistant" and content == (
                "Acknowledged the summary of our previous conversation history."
            ):
                continue
        messages.append(copy.deepcopy(item))
    examples = [item for item in messages if item.get("_no_save")]
    messages = [item for item in messages if not item.get("_no_save")]
    starts = []
    last_turn_id = None
    for i, item in enumerate(messages):
        if item.get("role") != "user":
            continue
        turn_id = item.get("turn_id")
        if not turn_id or turn_id != last_turn_id:
            starts.append(i)
        last_turn_id = turn_id
    if turns == 0:
        return examples
    if turns > 0 and len(starts) > turns:
        messages = messages[starts[-turns]:]
    while messages and messages[0].get("role") in {"tool", "_checkpoint"}:
        messages.pop(0)
    return examples + messages


def merge_archive(request, current):
    """Append only current-run messages to the original archive.

    Args:
        request: Request retaining the original archive for this run.
        current: New messages after historical messages were excluded from save.

    Returns:
        Complete history; the request archive and caller messages stay unchanged.
    """
    if getattr(request, "history_policy", "") != "recent_only":
        return current
    result = copy.deepcopy(getattr(request, "history_archive", None) or [])
    if not hasattr(request, "history_message_metadata"):
        request.history_message_metadata = {}
    turn_id = request.history_message_metadata.setdefault(-1, {"turn_id": uuid.uuid4().hex})["turn_id"]
    for index, message in enumerate(current):
        item = copy.deepcopy(message)
        if item.get("role") == "tool":
            original = getattr(request, "history_lookup_archive", {}).get(item.get("tool_call_id"))
            if original is not None:
                item["content"] = original
        if item.get("role") != "_checkpoint":
            metadata = request.history_message_metadata.setdefault(
                index, {"timestamp": time.time(), "message_id": uuid.uuid4().hex, "turn_id": turn_id}
            )
            for key, value in metadata.items():
                item.setdefault(key, value)
            conversation_id = getattr(getattr(request, "conversation", None), "cid", None)
            if conversation_id:
                item.setdefault("conversation_id", conversation_id)
        result.append(item)
    return result


class RecentOnlyCompressor:
    """Reduce request pressure through removable history, without an LLM summary."""

    preserve_current_turn = True

    def __init__(self, request=None):
        self.request = request
        self.example_turns = sum(
            item.get("role") == "user" and bool(item.get("_no_save"))
            for item in (getattr(request, "contexts", None) or [])
            if isinstance(item, dict)
        )

    def should_compress(self, messages, current_tokens, max_tokens):
        """Apply a conservative window margin to the current request."""
        return max_tokens > 0 and current_tokens > max_tokens * 0.8

    async def __call__(self, messages):
        """Replace old history lookups first, preserving current source material."""
        result = [item.model_copy(deep=True) for item in messages]
        history_calls = set()
        for item in result:
            for call in item.tool_calls or []:
                data = call if isinstance(call, dict) else call.model_dump()
                if data.get("function", {}).get("name") == "pc_read_chat_history":
                    history_calls.add(data["id"])
        lookups = [m for m in result if m.role == "tool" and m.tool_call_id in history_calls]
        for item in lookups[:-1]:
            if isinstance(item.content, str) and len(item.content) > 800:
                if self.request is not None and not item._no_save:
                    self.request.history_lookup_archive.setdefault(item.tool_call_id, item.content)
                item.content = (
                    "[Earlier history lookup removed from the active context budget. "
                    "The complete original remains available through pc_read_chat_history; "
                    "repeat its preceding arguments or use the returned pagination cursor.]"
                )
        starts = []
        last_turn_id = None
        for i, item in enumerate(result):
            if item.role != "user":
                continue
            if not item._turn_id or item._turn_id != last_turn_id:
                starts.append(i)
            last_turn_id = item._turn_id
        historical = [i for i in starts if result[i]._no_save][self.example_turns:]
        if historical:
            end = next((i for i in starts if i > historical[0]), len(result))
            del result[historical[0]:end]
        return result
