"""Tencent Cloud one-sentence speech recognition helpers."""

import base64
import datetime
import hashlib
import hmac
import io
import json
import wave

import requests


HOST = "asr.tencentcloudapi.com"
ENDPOINT = f"https://{HOST}"
SERVICE = "asr"
ACTION = "SentenceRecognition"
VERSION = "2019-06-14"


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _authorization(secret_id: str, secret_key: str, timestamp: int, body: str) -> str:
    date = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    hashed_body = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_body}"
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap mono signed 16-bit PCM in a WAV container."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return output.getvalue()


def transcribe_pcm(pcm_data: bytes, secret_id: str, secret_key: str) -> str:
    """Recognize a short 16 kHz mono PCM recording using Tencent Cloud ASR."""
    if not secret_id or not secret_key:
        raise ValueError("请先在设置中心填写腾讯云 SecretId 和 SecretKey。")
    if not pcm_data:
        raise ValueError("没有录到语音，请检查麦克风后重试。")

    wav_data = pcm_to_wav(pcm_data)
    if len(wav_data) > 3 * 1024 * 1024:
        raise ValueError("录音文件超过腾讯云一句话识别的大小限制，请缩短录音。")

    payload = {
        "EngSerViceType": "16k_zh",
        "SourceType": 1,
        "VoiceFormat": "wav",
        "Data": base64.b64encode(wav_data).decode("ascii"),
        "DataLen": len(wav_data),
        "FilterPunc": 0,
        "ConvertNumMode": 1,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    headers = {
        "Authorization": _authorization(secret_id, secret_key, timestamp, body),
        "Content-Type": "application/json; charset=utf-8",
        "Host": HOST,
        "X-TC-Action": ACTION,
        "X-TC-Version": VERSION,
        "X-TC-Timestamp": str(timestamp),
    }

    response = requests.post(ENDPOINT, data=body.encode("utf-8"), headers=headers, timeout=30)
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"腾讯云返回了无法读取的响应 (HTTP {response.status_code})。") from exc

    if response.status_code != 200:
        raise RuntimeError(f"腾讯云请求失败 (HTTP {response.status_code})。")
    api_response = result.get("Response", {})
    error = api_response.get("Error")
    if error:
        code = error.get("Code", "UnknownError")
        message = error.get("Message", "未知错误")
        raise RuntimeError(f"腾讯云识别失败：{code} - {message}")

    text = (api_response.get("Result") or "").strip()
    if not text:
        raise RuntimeError("没有识别出语音内容，请说得清楚一些后重试。")
    return text
