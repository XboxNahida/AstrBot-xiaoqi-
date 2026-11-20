"""语音识别配置与请求，音频只存内存，原腾讯云通道保留。"""

import base64
import copy
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

import config_manager
from companion_config import _secret, validate_endpoint
from companion_models import DirectModels
from perception.tencent_asr import pcm_to_wav

SPEECH_PRESETS = {
    "自定义 / 本机语音模型": "http://127.0.0.1:8000/v1",
    "OpenAI": "https://api.openai.com/v1",
    "小米 MiMo ASR": "https://api.xiaomimimo.com/v1",
}


class SpeechSettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(config_manager.CONFIG_FILE).with_name("speech_settings.json")

    @staticmethod
    def defaults():
        return {"engine": "tencent", "active_profile": "default", "profiles": [{
            "id": "default", "name": "我的语音模型", "preset": "自定义 / 本机语音模型",
            "provider": "openai", "base_url": "http://127.0.0.1:8000/v1", "key": "", "model": "",
        }]}

    @staticmethod
    def validate(cfg):
        if cfg.get("engine") not in ("tencent", "openai"):
            raise ValueError("请选择腾讯云或自定义语音识别")
        profiles = cfg.get("profiles")
        if not isinstance(profiles, list) or not profiles or not all(isinstance(p, dict) for p in profiles):
            raise ValueError("至少保留一套语音模型配置")
        ids = [p.get("id") for p in profiles]
        if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids) or cfg.get("active_profile") not in ids:
            raise ValueError("请选择有效的语音模型配置")
        for profile in profiles:
            for field in ("name", "key", "model", "base_url"):
                if not isinstance(profile.get(field), str):
                    raise ValueError("语音模型配置字段格式错误")  # noqa: TRY004 -- 配置校验对外统一ValueError。
            if not profile["name"].strip():
                raise ValueError("请填写语音模型配置名称")
            validate_endpoint(profile["base_url"])
            speech_protocol(profile)

    def load(self):
        if not self.path.exists():
            return self.defaults()
        try:
            cfg = json.loads(self.path.read_text(encoding="utf-8"))
            self.validate(cfg)
            for profile in cfg["profiles"]:
                profile["key"] = _secret(profile["key"], False)
            return cfg
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("语音模型配置读取失败，原文件已保留") from exc

    def save(self, cfg):
        self.validate(cfg)
        stored = copy.deepcopy(cfg)
        for profile in stored["profiles"]:
            profile["key"] = _secret(profile["key"], True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as target:
                json.dump(stored, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def active_profile(cfg):
    return next(p for p in cfg["profiles"] if p["id"] == cfg["active_profile"])


def model_base(profile):
    return validate_endpoint(profile["base_url"]).removesuffix("/audio/transcriptions").removesuffix("/chat/completions")


def speech_protocol(profile):
    protocol = profile.get("protocol", "auto")
    if protocol not in ("auto", "openai", "mimo"):
        raise ValueError("请选择有效的语音接口协议")
    if protocol != "auto":
        return protocol
    host = urlsplit(profile["base_url"]).hostname
    if host == "api.xiaomimimo.com" and profile.get("model", "").strip().lower() == "mimo-v2.5-asr":
        return "mimo"
    return "openai"


def list_models(profile):
    return DirectModels({**profile, "provider": "openai", "base_url": model_base(profile)}).models()


def check_ready(cfg, secret_id="", secret_key=""):
    if cfg["engine"] == "tencent":
        if not secret_id.strip() or not secret_key.strip():
            raise ValueError("请先填写腾讯云 SecretId 和 SecretKey，或切换到自定义语音模型")
    else:
        profile = active_profile(cfg)
        model_base(profile)
        if not profile.get("model", "").strip():
            raise ValueError("请填写自定义语音识别模型名称")


def transcribe(pcm_data, cfg, secret_id="", secret_key=""):
    check_ready(cfg, secret_id, secret_key)
    if not pcm_data:
        raise ValueError("没有录到语音，请检查麦克风后重试")
    if cfg["engine"] == "tencent":
        # 延迟取原实现，便于保持其签名与错误处理行为。
        from perception.tencent_asr import transcribe_pcm as tencent_transcribe
        return tencent_transcribe(pcm_data, secret_id, secret_key)
    if len(pcm_data) > 16000 * 2 * 55:
        raise ValueError("录音超过55秒，请缩短后重试")
    profile = active_profile(cfg)
    key = profile.get("key", "").strip()
    headers = {"Authorization": "Bearer " + key} if key else {}
    mimo = speech_protocol(profile) == "mimo"
    endpoint = "/chat/completions" if mimo else "/audio/transcriptions"
    wav = pcm_to_wav(pcm_data)
    if mimo:
        request = {"json": {"model": profile["model"].strip(), "stream": False, "messages": [{
            "role": "user", "content": [{"type": "input_audio", "input_audio": {
                "data": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"),
            }}],
        }]}}
    else:
        request = {"files": {"file": ("speech.wav", wav, "audio/wav")},
                   "data": {"model": profile["model"].strip(), "response_format": "json"}}
    try:
        response = requests.post(
            model_base(profile) + endpoint, headers=headers,
            timeout=(8, 90), allow_redirects=False, **request,
        )
    except requests.RequestException:
        raise ValueError(f"语音接口 {endpoint} 连接失败或超时，请检查地址与网络") from None
    if response.status_code != 200:
        raise ValueError(f"语音接口 {endpoint} 返回HTTP {response.status_code}，请检查协议、地址、密钥及模型")
    try:
        result = response.json()
    except ValueError:
        raise ValueError("语音接口未返回有效JSON") from None
    text = result.get("text") if isinstance(result, dict) else None
    if mimo:
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("语音接口没有返回识别文字，请检查音频和模型")
    return text.strip()
