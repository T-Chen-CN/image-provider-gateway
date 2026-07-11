"""Tests for structured provider error classification."""
from __future__ import annotations

import urllib.error

from image_provider_gateway.errors import (
    AUTH_FAILED,
    BAD_REQUEST,
    CONTENT_POLICY_VIOLATION,
    MODEL_NOT_FOUND,
    NETWORK_ERROR,
    PROVIDER_JSON_ERROR,
    QUOTA_EXCEEDED,
    RATE_LIMIT,
    SAFETY_VIOLATION,
    SERVER_ERROR,
    TIMEOUT,
    UNKNOWN,
    classify_http_error,
    classify_transport_error,
)


def test_safety_violation_from_400_with_moderation_message():
    error_body = {
        "error": {
            "type": "invalid_request_error",
            "code": "moderation_blocked",
            "message": "Your request was rejected by the safety system.",
        }
    }
    classified = classify_http_error(400, error_body)
    assert classified.code == SAFETY_VIOLATION
    assert classified.retryable is False
    assert classified.hint  # hint present
    assert classified.http_status == 400


def test_safety_violation_from_message_only():
    error_body = {"error": {"message": "The prompt violates safety_system policy."}}
    classified = classify_http_error(400, error_body)
    assert classified.code == SAFETY_VIOLATION


def test_rate_limit_maps_429_and_is_retryable():
    error_body = {"error": {"message": "Too many requests. Try again later."}}
    classified = classify_http_error(429, error_body)
    assert classified.code == RATE_LIMIT
    assert classified.retryable is True


def test_quota_prefers_over_rate_limit_when_message_hints_quota():
    error_body = {"error": {"code": "insufficient_quota", "message": "Your quota was exceeded."}}
    classified = classify_http_error(429, error_body)
    assert classified.code == QUOTA_EXCEEDED
    assert classified.retryable is False


def test_auth_failed_401():
    classified = classify_http_error(401, {"error": {"message": "Invalid API key."}})
    assert classified.code == AUTH_FAILED
    assert classified.retryable is False


def test_auth_failed_403():
    classified = classify_http_error(403, {"error": {"message": "Forbidden."}})
    assert classified.code == AUTH_FAILED
    assert classified.retryable is False


def test_quota_exceeded_402():
    classified = classify_http_error(402, {"error": {"message": "Payment required."}})
    assert classified.code == QUOTA_EXCEEDED
    assert classified.retryable is False


def test_bad_request_400_generic():
    classified = classify_http_error(400, {"error": {"message": "Invalid size parameter."}})
    assert classified.code == BAD_REQUEST
    assert classified.retryable is False


def test_model_not_found_404_with_model_message():
    classified = classify_http_error(
        404,
        {"error": {"type": "invalid_request_error", "message": "The model 'gpt-image-99' does not exist."}},
    )
    assert classified.code == MODEL_NOT_FOUND


def test_model_not_found_400_with_model_message():
    classified = classify_http_error(
        400,
        {"error": {"message": "Model 'gpt-image-99' is not supported"}},
    )
    assert classified.code == MODEL_NOT_FOUND


def test_server_error_5xx_is_retryable():
    for status in (500, 502, 503, 504):
        classified = classify_http_error(status, {"error": {"message": "server oops"}})
        assert classified.code == SERVER_ERROR
        assert classified.retryable is True


def test_unknown_falls_back_when_status_uncategorized():
    classified = classify_http_error(418, {"error": {"message": "teapot"}})
    assert classified.code == UNKNOWN
    assert classified.retryable is False


def test_transport_timeout_is_retryable():
    classified = classify_transport_error("timeout", TimeoutError("timed out"))
    assert classified.code == TIMEOUT
    assert classified.retryable is True
    assert classified.hint


def test_transport_network_is_retryable():
    classified = classify_transport_error("network", urllib.error.URLError("dns error"))
    assert classified.code == NETWORK_ERROR
    assert classified.retryable is True


def test_provider_json_error_is_retryable_for_transient_proxies():
    classified = classify_transport_error("provider_json_error", ValueError("not-json"))
    assert classified.code == PROVIDER_JSON_ERROR
    # Deliberately retryable: non-JSON often reflects an intermittent proxy blip.
    assert classified.retryable is True


def test_to_dict_shape_preserves_all_fields():
    classified = classify_http_error(
        400, {"error": {"code": "moderation_blocked", "message": "nope"}}, body_text="{...}"
    )
    payload = classified.to_dict()
    assert payload["code"] == SAFETY_VIOLATION
    assert payload["retryable"] is False
    assert payload["http_status"] == 400
    assert "hint" in payload
    assert "message" in payload
    assert "provider_raw" in payload


def test_content_policy_violation_marker_maps_correctly():
    # The classifier currently maps content_policy language to safety_violation for
    # non-retryability parity; when providers explicitly send content_policy_violation
    # as their code, we keep the safety-family classification.
    classified = classify_http_error(
        400, {"error": {"code": "content_policy_violation", "message": "Policy violated."}}
    )
    assert classified.code in {SAFETY_VIOLATION, CONTENT_POLICY_VIOLATION}
    assert classified.retryable is False


def test_provider_error_string_fallback():
    """When upstream sends a raw string (not JSON), we still classify by http_status."""
    classified = classify_http_error(429, "upstream throttled you")
    assert classified.code == RATE_LIMIT
    assert classified.message == "upstream throttled you"
