import copy
from types import SimpleNamespace

try:
    from task_context import context_budget, estimate_tokens, summary_slice
except ImportError:

    def context_budget(*args):
        raise AssertionError("Model-window task context budget is not implemented")

    estimate_tokens = summary_slice = context_budget


def test_budget_uses_selected_provider_not_model_name_guess():
    provider = SimpleNamespace(provider_config={"max_context_tokens": 65536})
    assert context_budget(provider) == 65536
    assert (
        context_budget(SimpleNamespace(provider_config={"model": "gemini-unknown"}))
        == 128000
    )


def test_metadata_and_configurable_fallback_resolve_without_mutating_provider():
    from astrbot.core.utils.llm_metadata import LLM_METADATAS

    provider = SimpleNamespace(
        provider_config={"max_context_tokens": 0},
        get_model=lambda: "native-context-test-model",
    )
    LLM_METADATAS["native-context-test-model"] = {
        "limit": {"context": 1050000, "output": 65536}
    }
    try:
        assert context_budget(provider) == 1050000
        assert provider.provider_config == {"max_context_tokens": 0}
    finally:
        LLM_METADATAS.pop("native-context-test-model", None)
    provider.provider_config["fallback_max_context_tokens"] = 32000
    assert context_budget(provider, fallback=64000) == 32000
    provider.provider_config.pop("fallback_max_context_tokens")
    assert context_budget(provider, fallback=64000) == 64000


def test_budget_counts_tools_images_system_and_reserves_output():
    contexts = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
    ]
    assert estimate_tokens(contexts, "system", [{"name": "tool"}]) > 4096
    assert estimate_tokens([], "x" * 1000, []) > estimate_tokens([], "", [])


def test_summary_only_removes_closed_rounds_and_keeps_original_input():
    messages = [{"role": "user", "content": "goal attachment-ref"}]
    for index in range(5):
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [{"id": str(index)}]},
                {"role": "tool", "tool_call_id": str(index), "content": "result"},
            ]
        )
    messages.append({"role": "assistant", "tool_calls": [{"id": "pending"}]})
    original = copy.deepcopy(messages)
    start, end = summary_slice(messages)
    assert start == 1
    assert end == 7
    assert messages[start:end][-1]["tool_call_id"] == "2"
    assert messages[end:][-1]["tool_calls"][0]["id"] == "pending"
    assert messages == original


def test_summary_never_consumes_missing_tool_reply():
    messages = [
        {"role": "user", "content": "goal"},
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "done"},
    ]
    assert summary_slice(messages) is None


def test_image_base64_bytes_are_not_counted_as_text_tokens():
    def messages(url):
        return [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ]

    assert estimate_tokens(
        messages("data:image/png;base64," + "x" * 1000000), "", []
    ) == estimate_tokens(messages("short-url"), "", [])


def test_old_rounds_can_compact_without_hiding_new_user_correction():
    messages = [{"role": "user", "content": "goal"}]
    for index in range(5):
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [{"id": str(index)}]},
                {"role": "tool", "tool_call_id": str(index), "content": "done"},
            ]
        )
        if index == 0:
            messages.append(
                {"role": "user", "content": "latest correction; attachment-ref"}
            )
    assert summary_slice(messages) == (1, 3)
