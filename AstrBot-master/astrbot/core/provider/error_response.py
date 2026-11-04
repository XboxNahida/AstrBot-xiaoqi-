import re
from typing import Any


def looks_like_upstream_llm_error_response(
    text: Any, *, require_error_envelope: bool = False
) -> bool:
    """Match high-confidence error envelopes returned as successful LLM text.

    Args:
        text: Provider response text to inspect.
        require_error_envelope: Require an explicit error heading so ordinary
            explanations and quoted errors remain valid assistant replies.

    Returns:
        Whether the text resembles an upstream failure response.
    """
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()[:2000]
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if require_error_envelope and not (
        re.match(
            r"^(?:all\W+(?:chat\W+models\W+failed|llm\W+providers\W+failed|"
            r"available\W+chat\W+models\W+are\W+unavailable)|"
            r"(?:the\W+)?prompt\W+(?:could\W*not\W+be\W+submitted|"
            r"was\W+not\W+submitted|contains\W+sensitive\W+words)|"
            r"unable\W+to\W+submit\W+request)(?:\s*$|[.:：。])",
            lowered,
        )
        or re.match(
            r"^(?:(?:llm|provider|api)\s+(?:response\s+)?error\s+|error\s+)?"
            r"(?:badrequesterror|apiconnectionerror|apistatuserror|"
            r"authenticationerror|permissiondeniederror|ratelimiterror|"
            r"notfounderror|internalservererror|api connection error|"
            r"api status error|authentication error|permission denied error|"
            r"rate limit error|internal server error)\s*(?:[:：-]|$)",
            lowered,
        )
        or re.match(r"^(?:模型|api|provider|函数工具|工具)\s*调用失败\s*[:：]", lowered)
        or lowered.startswith((
            "traceback (most recent call last):",
            "provider api error:",
        ))
        or (
            lowered.startswith(("{", "["))
            and re.search(r"[\"']error[\"']\s*:", lowered)
        )
    ):
        return False
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff_]+", " ", lowered).strip()
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff_]+", "", lowered)

    direct_markers = (
        "allchatmodelsfailed",
        "allllmprovidersfailed",
        "allavailablechatmodelsareunavailable",
        "promptcouldnotbesubmitted",
        "promptwasnotsubmitted",
        "promptcontainssensitivewords",
        "unabletosubmitrequest",
        "providerapierrorupstreamreturnedaninternalfailuremessage",
    )
    if any(marker in compact for marker in direct_markers):
        return True

    google_policy_features = (
        "generativeaiprohibitedusepolicy",
        "tryrephrasingtheprompt",
        "sensitivewords",
    )
    if sum(feature in compact for feature in google_policy_features) >= 2:
        return True

    if "functiondeclaration" in compact and any(
        marker in compact
        for marker in (
            "schemadidntspecify", "invalidrequest", "badrequest", "errorcode400",
        )
    ):
        return True
    if lowered.lstrip().startswith("traceback (most recent call last):"):
        return True
    if re.match(r"^(?:模型|api|provider|函数工具|工具)\s*调用失败\s*[:：]", cleaned, re.IGNORECASE):
        return True
    if re.match(
        r"^(?:api connection error|api status error|authentication error|"
        r"permission denied error|rate limit error|internal server error)\s*(?:[:：-]|$)",
        lowered,
    ):
        return True

    error_classes = (
        "badrequesterror", "apiconnectionerror", "apistatuserror", "authenticationerror",
        "permissiondeniederror", "ratelimiterror", "notfounderror", "internalservererror",
    )
    error_class = next((name for name in error_classes if name in compact), "")
    if error_class:
        structured_signal = any(
            marker in compact
            for marker in ("errorcode", "statuscode", "httpstatus", "requestid", "invalidrequest")
        )
        leading_error_class = bool(re.match(
            rf"^(?:(?:llm|provider|api)\s+(?:response\s+)?error\s+|error\s+)?{error_class}\b",
            normalized,
        ))
        if structured_signal or (
            leading_error_class
            and (":" in cleaned[:100] or "：" in cleaned[:100] or len(cleaned) <= 48)
        ):
            return True

    if lowered.lstrip().startswith(("{", "[")) and any(
        marker in lowered for marker in ('"error"', "'error'")
    ) and any(
        marker in compact
        for marker in ("invalid_request", "badrequest", "permissiondenied", "ratelimit")
    ):
        return True
    return False
