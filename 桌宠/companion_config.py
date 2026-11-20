"""桌宠独立配置：原子保存，多方案切换，Windows 用户密钥保护。"""

from __future__ import annotations

import base64
import copy
import ctypes
import ipaddress
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

PRESETS = {
    "自定义 / 本机模型": ("openai", "http://127.0.0.1:11434/v1"),
    "OpenAI": ("openai", "https://api.openai.com/v1"),
    "OpenRouter": ("openai", "https://openrouter.ai/api/v1"),
    "DeepSeek": ("openai", "https://api.deepseek.com/v1"),
    "Google Gemini": ("gemini", "https://generativelanguage.googleapis.com/v1beta"),
}


def validate_endpoint(value: str, *, websocket: bool = False) -> str:
    """校验远程加密地址，允许回环明文进行本机模型调用与测试。"""
    value = str(value).strip().rstrip("/")
    parts = urlsplit(value)
    secure, insecure = ("wss", "ws") if websocket else ("https", "http")
    if not parts.hostname or parts.scheme not in (secure, insecure):
        raise ValueError("请填写完整的" + ("WSS" if websocket else "HTTPS") + "地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("地址中不能包含用户名、密码、查询参数或片段")
    try:
        local = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        local = parts.hostname == "localhost"
    if parts.scheme == insecure and not local:
        raise ValueError("远程连接请使用加密地址，本机回环地址可以使用明文协议")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError("端口填写不正确") from exc
    return value


def _secret(value: str, encrypt: bool) -> str:
    """使用当前 Windows 账户 DPAPI 加解密，不写明文或可逆替代编码。"""
    if not value:
        return ""
    if os.name != "nt":
        raise ValueError("本版本的密钥保存需要Windows用户保护功能")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    if not encrypt and not value.startswith("dpapi:"):
        raise ValueError("配置中的密钥格式无效，原文件已保留")
    raw = (
        value.encode("utf-8") if encrypt else base64.b64decode(value[6:], validate=True)
    )
    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(Blob),
    ]
    function.restype = ctypes.c_int
    if not function(
        ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)
    ):
        raise ValueError("无法使用当前Windows账户解密或保存密钥，原配置已保留")
    try:
        result = ctypes.string_at(outgoing.data, outgoing.size)
        return (
            "dpapi:" + base64.b64encode(result).decode("ascii")
            if encrypt
            else result.decode("utf-8")
        )
    finally:
        kernel = ctypes.windll.kernel32
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(outgoing.data)


class SettingsStore:
    def __init__(self, path: str | Path | None = None):
        self.path = (
            Path(path) if path else Path(__file__).with_name("companion_settings.json")
        )

    @staticmethod
    def defaults() -> dict:
        return {
            "version": 1,
            "route": "direct",
            "active_profile": "default",
            "profiles": [
                {
                    "id": "default",
                    "name": "我的模型",
                    "preset": "自定义 / 本机模型",
                    "provider": "openai",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "",
                    "key": "",
                }
            ],
            "cloud": {
                "url": "",
                "device_id": "desktop-" + uuid.uuid4().hex[:12],
                "token": "",
            },
            "report_interval": 60,
            "report_foreground": False,
            "tts_enabled": False,
            "auto_mode": False,
            "critique_screenshot": False,
            "critique_cooldown": 45,
            "pet_name": "小七",
            "system_prompt": "你是小七，一位温暖自然的桌面伙伴。请简洁地用中文回应。",
        }

    @staticmethod
    def validate(config: dict) -> None:
        profiles = config.get("profiles", [])
        ids = [p.get("id") for p in profiles]
        if (
            not ids
            or len(set(ids)) != len(ids)
            or config.get("active_profile") not in ids
        ):
            raise ValueError("请选择有效的模型配置，至少保留一套配置")
        if config.get("route") not in ("original", "cloud", "direct"):
            raise ValueError("请选择原版通道、云端小七或本机直连")
        for profile in profiles:
            from model_transport import idle_timeout
            idle_timeout(profile)
            if type(profile.get('stream_usage', True)) is not bool:
                raise ValueError('流式用量统计必须为开启或关闭')
            if not profile.get("name", "").strip():
                raise ValueError("请给模型配置起一个名字")
            if profile.get("provider") not in ("openai", "gemini"):
                raise ValueError("请选择支持的服务商")
            validate_endpoint(profile.get("base_url", ""))
        cloud = config.get("cloud", {})
        if cloud.get("url"):
            validate_endpoint(cloud["url"], websocket=True)
        if (
            not isinstance(cloud.get("device_id"), str)
            or not cloud["device_id"].strip()
        ):
            raise ValueError("设备标识不能为空")
        interval = config.get("report_interval", 60)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 15 <= interval <= 3600
        ):
            raise ValueError("汇报间隔应为15至3600秒")
        cooldown = config.get("critique_cooldown", 45)
        if type(cooldown) is not int or not 15 <= cooldown <= 3600:
            raise ValueError("锐评冷却应为15至3600秒")
        for key in ("auto_mode", "critique_screenshot"):
            if type(config.get(key, False)) is not bool:
                raise ValueError("自动感知和截图开关必须为开启或关闭")

    def load(self) -> dict:
        if not self.path.exists():
            return self.defaults()
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
            defaults = self.defaults()
            for key in ("auto_mode", "critique_screenshot", "critique_cooldown"):
                config.setdefault(key, defaults[key])
            self.validate(config)
            for profile in config["profiles"]:
                profile["key"] = _secret(profile.get("key", ""), False)
            config["cloud"]["token"] = _secret(config["cloud"].get("token", ""), False)
            return config
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("配置读取失败，原文件已保留：" + str(exc)) from exc

    def save(self, config: dict) -> None:
        self.validate(config)
        stored = copy.deepcopy(config)
        for profile in stored["profiles"]:
            profile["key"] = _secret(profile.get("key", ""), True)
        stored["cloud"]["token"] = _secret(stored["cloud"].get("token", ""), True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            self.path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as target:
                json.dump(stored, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
