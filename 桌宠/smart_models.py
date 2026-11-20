"""自主视觉请求：完整人设、必需截图和服务端实际用量。"""

import re
from urllib.parse import quote, urlsplit

from brain.prompts import get_system_prompt
from companion_config import validate_endpoint
from companion_models import DirectModels


class SmartModelError(ValueError):
    """请求失败仍携带已经收到的实际用量，避免漏记账。"""

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class _ObservationModel(DirectModels):
    def __init__(self, profile, cfg):
        super().__init__(profile)
        self.cfg = cfg
        self.usage = {}
        self.finish_reason = None

    def _request(self, method, path, **kwargs):
        body = kwargs["json"]
        cfg = self.cfg
        generation = body.setdefault("generationConfig", {}) if self.gemini else body
        if cfg.get("short_output", True):
            if self.gemini:
                token_key = "maxOutputTokens"
            else:
                model = self.profile["model"].lower().rsplit("/", 1)[-1]
                token_key = "max_completion_tokens" if re.match(r"(?:o[134](?:-|$)|gpt-5)", model) else "max_tokens"
            generation[token_key] = cfg.get("max_output_tokens", 256)
        if cfg.get("low_reasoning", False):
            mode = cfg.get("reasoning_mode", "off")
            if mode not in ("off", "low"):
                raise ValueError("思考强度请选择关闭或低")
            model = self.profile['model'].lower().rsplit('/', 1)[-1]
            gemini3 = bool(re.match(r'gemini-3(?:[.-]|$)', model))
            # Gemini 3不能完全关思考：Flash最低minimal，Pro最低low；设置页明确标注。
            effort = ('minimal' if 'flash' in model else 'low') if gemini3 and mode == 'off' else 'low'
            if self.gemini:
                if gemini3:
                    generation['thinkingConfig'] = {'thinkingLevel': effort.upper()}
                elif mode == "low" and "gemini-4" in model:
                    generation["thinkingConfig"] = {"thinkingLevel": "low"}
                else:
                    generation["thinkingConfig"] = {"thinkingBudget": 0 if mode == "off" else 1024}
            else:
                generation["reasoning_effort"] = effort if gemini3 else ("none" if mode == "off" else "low")
        try:
            result = super()._request(method, path, **kwargs)
        except ValueError as exc:
            result = getattr(exc, 'result', {})
            usage = result.get('usageMetadata' if self.gemini else 'usage')
            self.usage = usage if isinstance(usage, dict) else {}
            raise
        usage = result.get("usageMetadata" if self.gemini else "usage")
        self.usage = usage if isinstance(usage, dict) else {}
        choices = result.get("candidates" if self.gemini else "choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finishReason" if self.gemini else "finish_reason")
            if reason in ("stop", "length", "content_filter", "tool_calls", "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER"):
                self.finish_reason = reason
        return result


class SmartModelClient:
    def __init__(self, original_controller, smart_cfg, bridge_config):
        self.original_controller = original_controller
        self.smart_cfg = dict(smart_cfg)
        self.bridge_config = bridge_config

    def validate_configuration(self, kind='passive'):
        """纯本地预检：未发送请求的配置错误不占用自主预算。"""
        bridge, smart = self.bridge_config, self.smart_cfg
        separate = smart.get('separate_model', False) and kind in ('user', 'summary')
        if not separate and bridge.get("route", "original") == "original":
            cfg, engine = self.original_controller.config_mgr, self.original_controller.engine
            provider = cfg.get("api_provider", "") or getattr(engine, "provider", "gemini")
            if provider.lower() in ("antigravity", "agy", "google_antigravity"):
                raise ValueError("Antigravity 原通道无法保证视觉限制，请选择独立视觉配置")
            if not (cfg.get("model", "") or getattr(engine, "model", "")):
                raise ValueError("请先选择视觉模型")
            if provider.lower() not in ("gemini", "google", "vertex", "vertex_ai", "google_cloud", "gcp"):
                validate_endpoint(cfg.get("custom_api_url", "https://openrouter.ai/api/v1"))
            return
        if not separate and bridge.get("route") == "cloud":
            raise ValueError("旧云端协议只支持文字，自主观察请在智能陪伴页选择独立视觉配置")
        profile_id = smart["profile_id"] if separate else bridge.get("active_profile")
        profile = next((p for p in bridge.get("profiles", []) if p.get("id") == profile_id), None)
        if not profile or not profile.get("model", "").strip():
            raise ValueError("请选择有效的自主视觉模型配置")
        validate_endpoint(profile.get("base_url", ""))

    def _original_profile(self):
        cfg = self.original_controller.config_mgr
        engine = self.original_controller.engine
        provider = cfg.get("api_provider", "").lower().strip()
        provider = provider or getattr(engine, "provider", "gemini")
        key = cfg.get("api_key", "").strip()
        model = cfg.get("model", "").strip() or getattr(engine, "model", "")
        timeout = cfg.get('api_timeout', 35)
        if provider in ("antigravity", "agy", "google_antigravity"):
            raise ValueError("Antigravity 原通道无法保证输出限制及用量统计，请选择独立视觉配置")
        if provider in ("vertex", "vertex_ai", "google_cloud", "gcp"):
            token, project, auth_key, _error = engine.vertex_auth.get_auth(
                creds_file=cfg.get("vertex_credentials_file", ""),
                project_id=cfg.get("vertex_project_id", ""), api_key=key)
            if not project or not (token or auth_key):
                raise ValueError("Vertex AI 凭据或项目未就绪，请检查原版设置")
            location = cfg.get("vertex_location", "global").strip() or "global"
            host = "aiplatform.googleapis.com" if location == "global" else location + "-aiplatform.googleapis.com"
            base = f"https://{host}/v1beta1/projects/{quote(project, safe='')}/locations/{quote(location, safe='')}/publishers/google"
            return {"provider": "gemini", "base_url": base, "model": model, "key": auth_key if not token else "", "access_token": token, "timeout_seconds": timeout, 'stream_usage': cfg.get('api_stream_usage', True)}
        gemini = provider in ("gemini", "google")
        return {"provider": "gemini" if gemini else "openai", "key": key,
                "model": model, "timeout_seconds": timeout, 'stream_usage': cfg.get('api_stream_usage', True), "base_url": "https://generativelanguage.googleapis.com/v1beta" if gemini else cfg.get("custom_api_url", "https://openrouter.ai/api/v1")}

    def _context(self, info):
        if not self.smart_cfg.get("context_enabled", True):
            return ""
        context = f"场景：{info.get('scene', 'desktop')}；窗口：{info.get('title', '')}；进程：{info.get('process', '')}"
        memory = getattr(self.original_controller.engine, "context_memory", None)
        if memory is not None:
            items = memory.get_recent_context(info.get("scene", "desktop"), info.get("title", ""), max_items=1)
            if items:
                context += "\n相关记忆：" + str(items[0])
        return context[:self.smart_cfg.get("context_chars", 400)]

    def generate(self, info: dict, image_b64: str | None, kind="passive", *, user_text="") -> dict:
        if kind != "summary" and (not isinstance(image_b64, str) or not image_b64.strip()):
            raise ValueError("桌面观察必须包含当前截图，本次未调用模型")
        cfg = self.original_controller.config_mgr
        system = get_system_prompt(cfg.get("pet_name", "哈Gemi"), cfg.get("catchphrases", None),
                                   cfg.get("custom_system_prompt", ""), persona_style=cfg.get("persona_style", "元气毒舌 (默认)"))
        bridge = self.bridge_config
        # 锐评/自主观察/破冰统一跟随主模型，不再被旧独立视觉配置分流。
        separate = self.smart_cfg.get("separate_model", False) and kind in ('user', 'summary')
        route = bridge.get("route", "original")
        if not separate and route == "cloud":
            raise ValueError("旧云端协议只支持文字，自主观察请在智能陪伴页选择独立视觉配置")
        if not separate and route == "original":
            profile = self._original_profile()
        else:
            profile_id = self.smart_cfg.get("profile_id") if separate else bridge.get("active_profile")
            profile = next((p for p in bridge.get("profiles", []) if p.get("id") == profile_id), None)
            if profile is None:
                raise ValueError("请选择有效的自主视觉模型配置")
        client = _ObservationModel(profile, self.smart_cfg)
        if profile.get("access_token"):
            client.headers["Authorization"] = "Bearer " + profile["access_token"]
        prompt = "结合当前截图自然回应，保留原人设及情绪标签。"
        if kind == "icebreaker":
            prompt = "结合当前截图自然开启一个话题，保留原人设及情绪标签。"
        elif kind == "manual":
            prompt = "立即锐评当前截图，保留原人设及情绪标签。"
        context = "" if kind == "summary" else self._context(info)
        if kind == "summary":
            prompt = user_text
        elif kind == "user":
            prompt = "结合当前截图回应主人的原话，保留原人设及情绪标签。\n" + context + "\n主人原话：\n" + user_text
        else:
            prompt += "\n" + context
        error, text = None, ""
        try:
            text = client.chat(prompt, system_prompt=system, image_b64=image_b64)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            error = exc
        finally:
            client.session.close()
        host = urlsplit(profile.get("base_url", "")).hostname or ""
        provider = {"api.deepseek.com": "deepseek", "openrouter.ai": "openrouter"}.get(host, host)
        result = {"text": text, "usage": client.usage, "usage_known": False,
                  "model": profile.get("model", ""),
                  "provider": provider or profile.get("provider", ""),
                  "request_info": {"kind": kind, "system_chars": len(system), "prompt_chars": len(prompt),
                                   "context_chars": len(context), "user_chars": len(user_text),
                                   "has_image": bool(image_b64), "image_base64_chars": len(image_b64 or "")}}
        keys = (("promptTokenCount", "input_tokens"), ("candidatesTokenCount", "output_tokens"), ("totalTokenCount", "total_tokens")) if client.gemini else (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens"))
        for source, target in keys:
            value = client.usage.get(source)
            if type(value) is int and value >= 0:
                result[target] = value
        prompt_details = client.usage.get("prompt_tokens_details", {})
        cached = client.usage.get("cachedContentTokenCount") if client.gemini else (
            prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None)
        if type(cached) is int and 0 <= cached <= result.get("input_tokens", 0):
            result["cached_input_tokens"] = cached
        details = client.usage.get("completion_tokens_details")
        thinking = client.usage.get("thoughtsTokenCount") if client.gemini else (
            details.get("reasoning_tokens") if isinstance(details, dict) else None)
        if type(thinking) is int and thinking >= 0:
            result["reasoning_tokens"] = thinking
        if client.finish_reason is not None:
            result["finish_reason"] = client.finish_reason
        result["usage_known"] = "total_tokens" in result
        if error is not None:
            message = str(error) if isinstance(error, ValueError) else "模型返回格式异常，本次未标记成功"
            if client.finish_reason in ("length", "MAX_TOKENS") and not text:
                budget = f"（请求上限{self.smart_cfg['max_output_tokens']} Token）" if self.smart_cfg.get("short_output", True) else ""
                message = "模型生成额度已耗尽" + budget + "，尚未生成正文。"
                if result.get("reasoning_tokens", 0):
                    message += f"已用于思考{result['reasoning_tokens']} Token。"
                message += "请调高总生成预算或选择支持关闭思考的模型；本次不自动重试。"
            raise SmartModelError(message, result) from error
        return result
