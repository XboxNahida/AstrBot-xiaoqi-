"""模型限制与稳定身份，纯数据逻辑，不保存凭据。"""

import hashlib
import json


def model_identity(profile):
    values = [profile.get(k, "") for k in ("provider", "base_url", "model", "scope")]
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _positive(value, maximum=4000000):
    return value if type(value) is int and 0 < value <= maximum else 0


def limits_for(profile, metadata=None, overrides=None):
    metadata, overrides = metadata or {}, overrides or {}
    name = str(profile.get("model", "")).lower().rsplit("/", 1)[-1]
    # 内置值故意低于部分新版本实际能力；自定义别名不会靠模糊关键字套大窗口。
    known = {
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4.1": 128000,
        "gpt-4.1-mini": 128000,
        "gpt-4.1-nano": 128000,
        "gemini-2.0-flash": 128000,
        "gemini-2.5-flash": 128000,
        "gemini-2.5-pro": 128000,
    }
    context = known.get(name, 8192)
    source = "内置保守值" if name in known else "未知模型保守值，请核对"
    input_limit = _positive(metadata.get("inputTokenLimit"))
    total = (
        _positive(metadata.get("context_length"))
        or _positive(metadata.get("context_window"))
        or _positive(metadata.get("max_model_len"))
    )
    output_max = (
        _positive(metadata.get("outputTokenLimit"))
        or _positive(metadata.get("max_output_tokens"))
        or _positive((metadata.get("top_provider") or {}).get("max_completion_tokens"))
    )
    if input_limit or total:
        context = total or (input_limit + (output_max or 2048))
        source = "服务商元数据"
    if overrides.get("context_tokens"):
        context = _positive(overrides["context_tokens"])
        if context < 1024:
            raise ValueError("上下文上限须为1024至4000000 Token")
        input_limit, source = 0, "手动配置"
    output = _positive(overrides.get("output_tokens")) or 2048
    output = min(output, output_max or output)
    safety = overrides.get("safety_percent", 15)
    edge = overrides.get(
        "image_edge", 1536 if source != "未知模型保守值，请核对" else 1024
    )
    quality = overrides.get("jpeg_quality", 85)
    if type(safety) is not int or not 5 <= safety <= 40:
        raise ValueError("安全余量须为5%至40%")
    if (
        type(edge) is not int
        or not 512 <= edge <= 4096
        or type(quality) is not int
        or not 60 <= quality <= 95
    ):
        raise ValueError("图片长边须为512至4096，JPEG质量须为60至95")
    if output >= context or output < 64:
        raise ValueError("输出预留须至少64 Token并小于上下文窗口")
    return {
        "context_tokens": context,
        "input_limit": input_limit,
        "output_tokens": output,
        "safety_percent": safety,
        "image_edge": edge,
        "jpeg_quality": quality,
        "source": source,
        "model": profile.get("model", ""),
        "max_messages": 200,
        "max_bytes": 10 * 1024 * 1024,
        "max_image_bytes": 1024 * 1024,
    }
