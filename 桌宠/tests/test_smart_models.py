"""智能观察只使用模拟服务，不向真实模型发送内容。"""

from types import SimpleNamespace

import pytest
import requests

from smart_config import SmartSettingsStore


def setup_client(monkeypatch, **changes):
    from smart_models import SmartModelClient

    calls = []
    response = {"choices": [{"message": {"content": "[curious] 完整回复"}}]}

    def post(session, url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: response)

    monkeypatch.setattr(requests.Session, "post", post)
    cfg = SmartSettingsStore.defaults()
    cfg.update(changes)
    original = SimpleNamespace(config_mgr={"custom_system_prompt": "完整人设" * 500})
    memory_calls = []
    original.engine = SimpleNamespace(context_memory=SimpleNamespace(
        get_recent_context=lambda *a, **kw: memory_calls.append((a, kw)) or ["相关记忆", "不得加入"]
    ))
    bridge = {"route": "direct", "active_profile": "one", "profiles": [
        {"id": "one", "provider": "openai", "base_url": "http://127.0.0.1:1234/v1", "model": "gpt-4o", "key": ""},
        {"id": "two", "provider": "gemini", "base_url": "https://example.com/v1beta", "model": "gemini-2.5-flash", "key": "test"},
    ]}
    return SmartModelClient(original, cfg, bridge), calls, response, memory_calls


def test_payload_preserves_full_persona_and_image_and_bounds_context(monkeypatch):
    client, calls, response, memory_calls = setup_client(monkeypatch, context_chars=90)
    response["usage"] = {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}
    result = client.generate({"title": "标题", "process": "code.exe", "scene": "work"}, "YWJj")
    body = calls[0][1]["json"]
    assert body["messages"][0]["content"] == "完整人设" * 500
    content = body["messages"][1]["content"]
    assert len(content[0]["text"].split("\n", 1)[1]) <= 90
    assert "相关记忆" in content[0]["text"] and "不得加入" not in content[0]["text"]
    assert content[1]["image_url"]["url"].endswith("YWJj")
    assert body["max_tokens"] == 256 and "reasoning_effort" not in body
    assert memory_calls[0][1]["max_items"] == 1
    assert result == {"text": "[curious] 完整回复", "usage": response["usage"],
                      "model": "gpt-4o", "provider": "127.0.0.1",
                      "usage_known": True, "input_tokens": 50, "output_tokens": 7, "total_tokens": 57,
                      "request_info": {"kind": "passive", "system_chars": 2000,
                                       "prompt_chars": len(content[0]["text"]),
                                       "context_chars": len(content[0]["text"].split("\n", 1)[1]),
                                       "user_chars": 0, "has_image": True, "image_base64_chars": 4}}


def test_cost_identity_and_cached_input_are_actual_metadata(monkeypatch):
    client, _calls, response, _memory = setup_client(monkeypatch)
    client.bridge_config["profiles"][0].update(base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    response["usage"] = {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35,
                         "prompt_tokens_details": {"cached_tokens": 20}}
    result = client.generate({}, "YWJj")
    assert result["provider"] == "deepseek" and result["model"] == "deepseek-chat"
    assert result["cached_input_tokens"] == 20


def test_empty_image_and_cloud_route_never_make_request(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch)
    with pytest.raises(ValueError, match="截图"):
        client.generate({}, " ")
    client.bridge_config["route"] = "cloud"
    with pytest.raises(ValueError, match="独立视觉"):
        client.generate({}, "YWJj")
    assert calls == []


def test_original_invalid_endpoint_is_rejected_by_local_preflight(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch)
    client.bridge_config["route"] = "original"
    client.original_controller.config_mgr.update(api_provider="openai_compatible", model="test", custom_api_url="not-a-url")
    with pytest.raises(ValueError):
        client.validate_configuration()
    assert not calls


@pytest.mark.parametrize("model,token_key", [("gpt-4o", "max_tokens"), ("o3", "max_completion_tokens"), ("gpt-5-mini", "max_completion_tokens")])
def test_openai_generation_parameters_and_unknown_usage(monkeypatch, model, token_key):
    client, calls, _, _ = setup_client(monkeypatch, low_reasoning=True, reasoning_mode="low")
    client.bridge_config["profiles"][0]["model"] = model
    result = client.generate({}, "YWJj")
    body = calls[0][1]["json"]
    assert body[token_key] == 256 and body["reasoning_effort"] == "low"
    assert result["usage_known"] is False and "total_tokens" not in result


@pytest.mark.parametrize("mode,budget", [("off", 0), ("low", 1024)])
def test_separate_gemini_keeps_image_limits_and_usage(monkeypatch, mode, budget):
    client, calls, response, _ = setup_client(monkeypatch, separate_model=True, profile_id="two", low_reasoning=True, reasoning_mode=mode)
    client.bridge_config["route"] = "cloud"
    response.clear()
    response.update(candidates=[{"content": {"parts": [{"text": "[confident] 回复"}]}}], usageMetadata={"promptTokenCount": 11, "candidatesTokenCount": 3, "totalTokenCount": 14})
    result = client.generate({}, "YWJj", 'user')
    body = calls[0][1]["json"]
    assert body["generationConfig"] == {"maxOutputTokens": 256, "thinkingConfig": {"thinkingBudget": budget}}
    assert body["contents"][0]["parts"][1]["inline_data"]["data"] == "YWJj"
    assert result["usage_known"] and result["total_tokens"] == 14


def test_original_route_reads_original_openai_settings(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch, short_output=False, context_enabled=False)
    client.bridge_config["route"] = "original"
    client.original_controller.config_mgr.update(api_provider="openai_compatible", custom_api_url="https://original.example/v1/chat/completions", api_key="private-test", model="original-model")
    client.generate({}, "YWJj")
    assert calls[0][0] == "https://original.example/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer private-test"
    assert calls[0][1]["json"]["model"] == "original-model"
    assert "max_tokens" not in calls[0][1]["json"]


def test_unsupported_parameters_fail_once_without_paid_retry(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch, low_reasoning=True)

    def reject(session, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=400)

    monkeypatch.setattr(requests.Session, "post", reject)
    with pytest.raises(ValueError, match="400"):
        client.generate({}, "YWJj")
    assert len(calls) == 1


def test_original_vertex_uses_credentials_and_preserves_image(monkeypatch):
    client, calls, response, _ = setup_client(monkeypatch)
    client.bridge_config["route"] = "original"
    client.original_controller.config_mgr.update(api_provider="vertex", model="gemini-3-flash", vertex_location="global")
    client.original_controller.engine.vertex_auth = SimpleNamespace(get_auth=lambda **kw: ("token-test", "project-test", "", ""))
    response.clear()
    response.update(candidates=[{"content": {"parts": [{"text": "[curious] Vertex回复"}]}}])
    result = client.generate({}, "YWJj")
    assert calls[0][0] == "https://aiplatform.googleapis.com/v1beta1/projects/project-test/locations/global/publishers/google/models/gemini-3-flash:streamGenerateContent?alt=sse"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer token-test"
    assert calls[0][1]["json"]["contents"][0]["parts"][1]["inline_data"]["data"] == "YWJj"
    assert result["usage_known"] is False


def test_invalid_profile_and_antigravity_fail_before_request(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch, separate_model=True, profile_id="missing")
    with pytest.raises(ValueError, match="有效"):
        client.generate({}, "YWJj", 'user')
    client.smart_cfg["separate_model"] = False
    client.bridge_config["route"] = "original"
    client.original_controller.config_mgr["api_provider"] = "antigravity"
    with pytest.raises(ValueError, match="独立视觉"):
        client.generate({}, "YWJj")
    assert calls == []


def test_provider_missing_usage_fields_are_not_zero_estimates(monkeypatch):
    client, _, response, _ = setup_client(monkeypatch)
    response["usage"] = {"prompt_tokens": 10, "total_tokens": "unknown"}
    result = client.generate({}, "YWJj")
    assert result["input_tokens"] == 10
    assert result["usage_known"] is False
    assert "output_tokens" not in result and "total_tokens" not in result


def test_gemini3_native_uses_supported_lowest_pro_thinking(monkeypatch):
    client, calls, response, _ = setup_client(monkeypatch, separate_model=True, profile_id="two", low_reasoning=True, reasoning_mode="off")
    client.bridge_config["profiles"][1]["model"] = "gemini-3-pro"
    response.clear()
    response.update(candidates=[{"content": {"parts": [{"text": "[curious] 回复"}]}}])
    client.generate({}, "YWJj", 'user')
    assert calls[0][1]["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}


@pytest.mark.parametrize('provider,model,mode,expected', [
    ('openai', 'models/gemini-3.5-flash-lite', 'off', 'minimal'),
    ('openai', 'gemini-3-flash-preview', 'off', 'minimal'),
    ('openai', 'gemini-3.1-pro-preview', 'off', 'low'),
    ('openai', 'models/gemini-3.5-flash-lite', 'low', 'low'),
    ('openai', 'gemini-4-hydrogen', 'off', 'none'),
    ('gemini', 'models/gemini-3.5-flash-lite', 'off', 'MINIMAL'),
    ('gemini', 'models/gemini-3.5-flash-lite', 'low', 'LOW'),
])
def test_gemini3_reasoning_compatibility_keeps_request_and_other_models(monkeypatch, provider, model, mode, expected):
    client, calls, response, _ = setup_client(monkeypatch, low_reasoning=True, reasoning_mode=mode)
    client.bridge_config['profiles'][0].update(provider=provider, model=model)
    if provider == 'gemini':
        response.clear()
        response.update(candidates=[{'content': {'parts': [{'text': '回复'}]}}])
    client.generate({}, 'YWJj', 'manual')
    assert len(calls) == 1
    body = calls[0][1]['json']
    if provider == 'gemini':
        assert body['generationConfig']['thinkingConfig'] == {'thinkingLevel': expected}
        assert body['contents'][0]['parts'][1]['inline_data']['data'] == 'YWJj'
    else:
        assert body['reasoning_effort'] == expected
        assert body['model'] == model
        assert body['messages'][-1]['content'][1]['image_url']['url'].endswith('YWJj')


def test_manual_request_uses_compact_context_and_same_generation_rules(monkeypatch):
    client, calls, _, memory = setup_client(monkeypatch, low_reasoning=True, reasoning_mode="low", context_chars=80)
    result = client.generate({"title": "立即锐评", "scene": "video"}, "YWJj", "manual")
    body = calls[0][1]["json"]
    assert body["max_tokens"] == 256 and body["reasoning_effort"] == "low"
    text = body["messages"][1]["content"][0]["text"]
    assert "立即" in text and "前情上下文连续记忆" not in text
    assert len(text.split("\n", 1)[1]) <= 80 and memory[0][1]["max_items"] == 1
    assert result["request_info"]["system_chars"] == 2000
    assert result["request_info"]["has_image"] is True


def test_user_text_is_preserved_once_while_only_added_context_is_bounded(monkeypatch):
    client, calls, _, _ = setup_client(monkeypatch, low_reasoning=True, context_chars=80)
    comment = "完整用户消息" * 300
    client.generate({"title": "互动窗口"}, "YWJj", "user", user_text=comment)
    body = calls[0][1]["json"]
    prompt = body["messages"][1]["content"][0]["text"]
    assert prompt.count(comment) == 1
    assert body["messages"][0]["content"] == "完整人设" * 500
    assert body["max_tokens"] == 256 and body["reasoning_effort"] == "none"
    assert len(prompt) - len(comment) < 180


def test_summary_is_text_only_and_not_truncated_by_scene_context_limit(monkeypatch):
    client, calls, _, memory = setup_client(monkeypatch, context_chars=50, low_reasoning=True)
    facts = "完整事实输入" * 300
    client.generate({}, None, "summary", user_text=facts)
    body = calls[0][1]["json"]
    assert body["messages"][1]["content"] == facts
    assert body["max_tokens"] == 256 and body["reasoning_effort"] == "none"
    assert not memory
