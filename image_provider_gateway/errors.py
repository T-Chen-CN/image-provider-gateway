"""Structured provider error classification.

The gateway historically surfaced ``result.error`` as a coarse string like
``http_error`` / ``timeout`` and left the raw upstream error body in
``provider_error``.  Downstream agents had to string-match on the message to
decide whether to retry or how to react.

This module classifies the raw upstream error into a stable enum-like ``code``
along with a ``retryable`` boolean and an optional actionable ``hint``.  The
classification is opportunistic: when we cannot confidently identify the
category we fall back to ``unknown`` and leave ``retryable`` unset.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# Enumerated error codes.  Kept as strings so JSON consumers can pattern-match.
SAFETY_VIOLATION = "safety_violation"
CONTENT_POLICY_VIOLATION = "content_policy_violation"
RATE_LIMIT = "rate_limit"
AUTH_FAILED = "auth_failed"
QUOTA_EXCEEDED = "quota_exceeded"
BAD_REQUEST = "bad_request"
MODEL_NOT_FOUND = "model_not_found"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
NETWORK_ERROR = "network_error"
PROVIDER_JSON_ERROR = "provider_json_error"
UNKNOWN = "unknown"


RETRYABLE_CODES: frozenset[str] = frozenset({
    RATE_LIMIT,
    SERVER_ERROR,
    TIMEOUT,
    NETWORK_ERROR,
    PROVIDER_JSON_ERROR,
})

HINTS: dict[str, str] = {
    SAFETY_VIOLATION: (
        "Provider safety filter rejected the prompt or reference image. "
        "Remove or soften safety-sensitive keywords (e.g. nude, sexual, minor, "
        "weapon, gore, boudoir), avoid images depicting real minors, and retry."
    ),
    CONTENT_POLICY_VIOLATION: (
        "Prompt or reference violates the provider's content policy. Rewrite the "
        "prompt in more neutral terms and ensure references contain no policy-"
        "restricted content."
    ),
    RATE_LIMIT: (
        "Rate-limited by the provider. Reduce concurrency, wait and retry, or "
        "check whether your account is on a low-tier rate plan."
    ),
    AUTH_FAILED: (
        "Provider rejected the API key. Verify the key is correct and active, "
        "and that the base URL matches the account that owns the key."
    ),
    QUOTA_EXCEEDED: (
        "Provider account is out of quota or credit. Top up the account or "
        "switch to a provider entry with available balance."
    ),
    MODEL_NOT_FOUND: (
        "The requested model is not available on this provider. Check available "
        "models and pass a supported model via --model."
    ),
    BAD_REQUEST: (
        "Provider rejected the request payload. Check size, quality, model name, "
        "and input image formats."
    ),
    SERVER_ERROR: "Provider returned a server error. Retry after a short backoff.",
    TIMEOUT: "Request timed out. Retry, or increase --timeout for slow models.",
    NETWORK_ERROR: "Network transport failed before a response arrived. Retry.",
    PROVIDER_JSON_ERROR: (
        "Provider returned a non-JSON response. Inspect provider_raw for the "
        "response preview; this often indicates a misconfigured base URL."
    ),
    UNKNOWN: "See provider_raw for full upstream response.",
}


@dataclass
class ClassifiedError:
    code: str
    retryable: bool
    message: str
    http_status: int | None = None
    hint: str | None = None
    provider_raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.hint is not None:
            payload["hint"] = self.hint
        if self.provider_raw is not None:
            payload["provider_raw"] = self.provider_raw
        return payload


def _lower(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    return ""


def _looks_like_safety(error_obj: Mapping[str, Any]) -> bool:
    tokens = " ".join([
        _lower(error_obj.get("type")),
        _lower(error_obj.get("code")),
        _lower(error_obj.get("message")),
        _lower(error_obj.get("param")),
    ])
    safety_markers = (
        "safety",
        "moderation",
        "safety_system",
        "safety system",
        "safety_violation",
        "content_policy",
        "content policy",
        "rejected_by_safety",
        "unsafe",
    )
    return any(marker in tokens for marker in safety_markers)


def _looks_like_model_not_found(error_obj: Mapping[str, Any]) -> bool:
    tokens = " ".join([
        _lower(error_obj.get("type")),
        _lower(error_obj.get("code")),
        _lower(error_obj.get("message")),
    ])
    if "model" not in tokens:
        return False
    return any(marker in tokens for marker in ("not found", "does not exist", "unknown", "unsupported", "not supported", "invalid model"))


def _looks_like_quota(error_obj: Mapping[str, Any]) -> bool:
    tokens = " ".join([
        _lower(error_obj.get("code")),
        _lower(error_obj.get("type")),
        _lower(error_obj.get("message")),
    ])
    return any(marker in tokens for marker in ("insufficient_quota", "quota", "billing", "balance", "credits"))


def _normalize_error_object(provider_error: Any) -> Mapping[str, Any]:
    """Return a plain mapping representing the upstream error object.

    Providers wrap errors inconsistently: sometimes ``{"error": {...}}``,
    sometimes flat ``{"code": ..., "message": ...}``, sometimes just a raw
    string.  We normalize to a mapping when possible.
    """
    if isinstance(provider_error, dict):
        inner = provider_error.get("error")
        if isinstance(inner, dict):
            return inner
        return provider_error
    return {}


def _preview(text: str, limit: int = 500) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def classify_http_error(
    http_status: int | None,
    provider_error: Any,
    *,
    body_text: str | None = None,
) -> ClassifiedError:
    """Classify an HTTP-level error from the image provider."""
    error_obj = _normalize_error_object(provider_error)
    message_raw = error_obj.get("message") if isinstance(error_obj, Mapping) else None
    if isinstance(message_raw, str) and message_raw:
        message = message_raw
    elif isinstance(provider_error, str):
        message = provider_error
    elif body_text:
        message = _preview(body_text)
    else:
        message = f"HTTP {http_status}" if http_status else "provider error"

    code = UNKNOWN
    if http_status == 400:
        if _looks_like_safety(error_obj):
            code = SAFETY_VIOLATION
        elif _looks_like_model_not_found(error_obj):
            code = MODEL_NOT_FOUND
        elif _looks_like_quota(error_obj):
            code = QUOTA_EXCEEDED
        else:
            code = BAD_REQUEST
    elif http_status in (401, 403):
        code = AUTH_FAILED
    elif http_status == 402:
        code = QUOTA_EXCEEDED
    elif http_status == 404:
        if _looks_like_model_not_found(error_obj):
            code = MODEL_NOT_FOUND
        else:
            code = BAD_REQUEST
    elif http_status == 429:
        if _looks_like_quota(error_obj):
            code = QUOTA_EXCEEDED
        else:
            code = RATE_LIMIT
    elif http_status is not None and 500 <= http_status < 600:
        code = SERVER_ERROR
    else:
        # Fall back to content-based hints when the status alone is not enough.
        if _looks_like_safety(error_obj):
            code = SAFETY_VIOLATION
        elif _looks_like_quota(error_obj):
            code = QUOTA_EXCEEDED

    return ClassifiedError(
        code=code,
        retryable=code in RETRYABLE_CODES,
        message=message,
        http_status=http_status,
        hint=HINTS.get(code),
        provider_raw=provider_error if provider_error else None,
    )


def classify_transport_error(kind: str, exc: BaseException) -> ClassifiedError:
    """Classify a transport-level failure (timeout / connection error)."""
    if kind == "timeout":
        code = TIMEOUT
    elif kind == "network":
        code = NETWORK_ERROR
    elif kind == "provider_json_error":
        code = PROVIDER_JSON_ERROR
    else:
        code = UNKNOWN
    return ClassifiedError(
        code=code,
        retryable=code in RETRYABLE_CODES,
        message=repr(exc),
        hint=HINTS.get(code),
    )
