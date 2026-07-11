"""Integration test: the provider adapter emits classified error payloads.

This test complements the unit-level ``test_error_codes.py`` by ensuring the
``OpenAIImagesProvider`` populates ``result.provider_error`` with the classified
dict (containing ``code``/``retryable``/``hint``) and that the gateway retry
logic honours the ``retryable`` field.
"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from image_provider_gateway.errors import (
    RATE_LIMIT,
    SAFETY_VIOLATION,
    SERVER_ERROR,
)
from image_provider_gateway.gateway import generate_image, generate_images_batch
from image_provider_gateway.models import ImageRequest


def _http_error(status: int, body: dict | str) -> urllib.error.HTTPError:
    payload = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError(
        url="https://example.invalid/images/generations",
        code=status,
        msg="Bad",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(payload.encode("utf-8")),
    )


def test_single_request_surfaces_classified_error(monkeypatch, tmp_path):
    def raise_400(*_a, **_k):
        raise _http_error(
            400,
            {"error": {"code": "moderation_blocked", "message": "Rejected by safety system."}},
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_400)

    result = generate_image(
        ImageRequest(id="safety", prompt="anything"),
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
    )
    assert result.ok is False
    assert result.error == "http_error"
    assert isinstance(result.provider_error, dict)
    assert result.provider_error["code"] == SAFETY_VIOLATION
    assert result.provider_error["retryable"] is False
    assert "hint" in result.provider_error


def test_batch_does_not_retry_when_classified_non_retryable(monkeypatch, tmp_path):
    """A safety-violation must fail immediately without retrying."""
    call_count = {"n": 0}

    def always_400(*_a, **_k):
        call_count["n"] += 1
        raise _http_error(400, {"error": {"code": "moderation_blocked", "message": "safety_system"}})

    monkeypatch.setattr("urllib.request.urlopen", always_400)
    result = generate_images_batch(
        [ImageRequest(id="safety", prompt="p")],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=2,
        qa_enabled=False,
        job_id="job-safety-noretry",
    )
    assert result.ok is False
    # Should try exactly once for a non-retryable classified error.
    assert call_count["n"] == 1
    failure = result.results[0]
    assert failure.provider_error["code"] == SAFETY_VIOLATION
    assert failure.provider_error["retryable"] is False


def test_batch_retries_rate_limit(monkeypatch, tmp_path):
    """A 429 rate-limit response is retryable and should hit the retry budget."""
    call_count = {"n": 0}

    def always_429(*_a, **_k):
        call_count["n"] += 1
        raise _http_error(429, {"error": {"message": "Too many requests"}})

    monkeypatch.setattr("urllib.request.urlopen", always_429)
    result = generate_images_batch(
        [ImageRequest(id="ratelimited", prompt="p")],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=2,
        qa_enabled=False,
        job_id="job-ratelimit",
    )
    assert result.ok is False
    # 1 initial + 2 retries = 3 attempts
    assert call_count["n"] == 3
    failure = result.results[0]
    assert failure.provider_error["code"] == RATE_LIMIT
    assert failure.provider_error["retryable"] is True


def test_server_error_5xx_is_retryable(monkeypatch, tmp_path):
    call_count = {"n": 0}

    def always_503(*_a, **_k):
        call_count["n"] += 1
        raise _http_error(503, {"error": {"message": "backend down"}})

    monkeypatch.setattr("urllib.request.urlopen", always_503)
    result = generate_images_batch(
        [ImageRequest(id="server-down", prompt="p")],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=1,
        qa_enabled=False,
        job_id="job-server-error",
    )
    assert result.ok is False
    assert call_count["n"] == 2  # initial + 1 retry
    failure = result.results[0]
    assert failure.provider_error["code"] == SERVER_ERROR
    assert failure.provider_error["retryable"] is True
