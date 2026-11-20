"""思考耗尽总生成预算时明确报错，不显示推理正文或自动重试。"""

import pytest

from smart_models import SmartModelError
from tests.test_smart_models import setup_client


def test_reasoning_only_truncation_explains_budget_and_keeps_usage(monkeypatch):
    client, calls, response, _ = setup_client(monkeypatch, low_reasoning=True, reasoning_mode="low")
    response.update(choices=[{"finish_reason": "length", "message": {
        "content": None, "reasoning_content": "不允许作为气泡显示的内部推理",
    }}], usage={"prompt_tokens": 2816, "completion_tokens": 256, "total_tokens": 3072,
               "completion_tokens_details": {"reasoning_tokens": 252}})
    with pytest.raises(SmartModelError) as failure:
        client.generate({}, "YWJj", "manual")
    assert "思考" in str(failure.value) and "256" in str(failure.value)
    assert "不允许作为气泡显示" not in str(failure.value)
    assert failure.value.result["reasoning_tokens"] == 252
    assert failure.value.result["finish_reason"] == "length"
    assert failure.value.result["total_tokens"] == 3072
    assert len(calls) == 1


def test_gemini_thinking_truncation_uses_native_fields(monkeypatch):
    client, calls, response, _ = setup_client(monkeypatch, separate_model=True, profile_id="two")
    response.clear()
    response.update(candidates=[{"finishReason": "MAX_TOKENS", "content": {"parts": [
        {"text": "不要泄露的内部推理", "thought": True},
    ]}}], usageMetadata={"promptTokenCount": 100, "candidatesTokenCount": 0,
                         "thoughtsTokenCount": 256, "totalTokenCount": 356})
    with pytest.raises(SmartModelError) as failure:
        client.generate({}, "YWJj", 'user')
    assert "思考" in str(failure.value)
    assert failure.value.result["reasoning_tokens"] == 256
    assert failure.value.result["finish_reason"] == "MAX_TOKENS"
    assert failure.value.result["total_tokens"] == 356 and len(calls) == 1


def test_truncated_visible_reply_remains_usable_and_does_not_retry(monkeypatch):
    client, calls, response, _ = setup_client(monkeypatch)
    response.update(choices=[{"finish_reason": "length", "message": {"content": "[curious] 已有正文"}}],
                    usage={"completion_tokens_details": {"reasoning_tokens": 64}})
    result = client.generate({}, "YWJj")
    assert result["text"] == "[curious] 已有正文"
    assert result["finish_reason"] == "length" and result["reasoning_tokens"] == 64
    assert len(calls) == 1


def test_saved_diagnostics_keep_only_known_reason_and_numeric_thinking(tmp_path):
    from smart_config import SmartSettingsStore
    from smart_policy import SmartPolicy

    policy = SmartPolicy(SmartSettingsStore.defaults(), tmp_path / "state.json")
    result = {"usage_known": True, "input_tokens": 2816, "output_tokens": 256,
              "total_tokens": 3072, "reasoning_tokens": 252, "finish_reason": "length"}
    policy.complete(result, False, 1000)
    fresh = SmartPolicy(policy.cfg, policy.path)
    assert fresh.state["last_request"]["reasoning_tokens"] == 252
    assert fresh.state["last_request"]["finish_reason"] == "length"
    policy.complete({"finish_reason": "服务商不可信原文", "reasoning_tokens": "原文"}, False, 1001)
    assert "服务商不可信原文" not in policy.path.read_text(encoding="utf-8")
