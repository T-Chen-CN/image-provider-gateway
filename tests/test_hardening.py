from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from image_provider_gateway.gateway import generate_image, generate_images_batch
from image_provider_gateway.image_probe import detect_image
from image_provider_gateway.models import ImageRequest, ImageResult
from image_provider_gateway.providers.openai_images import OpenAIImagesProvider


class _Response:
    status = 200

    def __init__(self, image_bytes: bytes):
        self.image_bytes = image_bytes

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        payload = base64.b64encode(self.image_bytes).decode("ascii")
        return json.dumps({"data": [{"b64_json": payload}]}).encode("utf-8")


class _RawResponse:
    status = 200

    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.raw


def _image_bytes(format_name: str, size: tuple[int, int] = (7, 5)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format=format_name)
    return buffer.getvalue()


class _MixedOutcomeProvider:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate(self, request: ImageRequest, _output_dir: Path, attempt: int = 1) -> ImageResult:
        if request.id == "raises":
            raise FileNotFoundError("missing reference")
        return ImageResult(
            ok=True,
            id=request.id,
            provider=request.provider,
            model=request.model,
            requested_size=request.size,
            mode=request.mode,
            path=f"/tmp/{request.id}.png",
            actual_size="1x1",
            format_detected="PNG",
            attempt=attempt,
        )


def test_batch_isolates_task_exceptions_and_writes_completion_records(monkeypatch, tmp_path):
    monkeypatch.setattr("image_provider_gateway.gateway.OpenAIImagesProvider", _MixedOutcomeProvider)

    result = generate_images_batch(
        [ImageRequest(id="ok", prompt="ok"), ImageRequest(id="raises", prompt="bad")],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=0,
        qa_enabled=False,
        job_id="job",
    )

    assert [item.ok for item in result.results] == [True, False]
    assert result.results[1].error == "FileNotFoundError"
    assert result.manifest_path.exists()
    events = [json.loads(line)["event"] for line in result.events_path.read_text().splitlines()]
    assert events[-1] == "batch_completed"


def test_batch_isolates_unsupported_provider(monkeypatch, tmp_path):
    monkeypatch.setattr("image_provider_gateway.gateway.OpenAIImagesProvider", _MixedOutcomeProvider)

    result = generate_images_batch(
        [
            ImageRequest(id="ok", prompt="ok"),
            ImageRequest(id="unsupported", prompt="bad", provider="unknown"),
        ],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=0,
        qa_enabled=False,
        job_id="job",
    )

    assert result.results[0].ok is True
    assert result.results[1].ok is False
    assert result.results[1].error == "unsupported_provider"


def test_batch_rejects_duplicate_request_ids(tmp_path):
    requests = [ImageRequest(id="dup", prompt="one"), ImageRequest(id="dup", prompt="two")]
    with pytest.raises(ValueError, match="unique"):
        generate_images_batch(requests, tmp_path, api_key="test", base_url="https://example.invalid")


@pytest.mark.parametrize("job_id", ["../escape", "/tmp/escape", "nested/job"])
def test_batch_rejects_unsafe_job_id(tmp_path, job_id):
    with pytest.raises(ValueError, match="job_id"):
        generate_images_batch(
            [ImageRequest(id="ok", prompt="ok")],
            tmp_path,
            api_key="test",
            base_url="https://example.invalid",
            job_id=job_id,
        )


@pytest.mark.parametrize("output_name", ["../escape", "/tmp/escape", "nested/name"])
def test_single_rejects_unsafe_output_name(tmp_path, output_name):
    with pytest.raises(ValueError, match="output_name"):
        generate_image(
            ImageRequest(id="ok", prompt="ok", output_name=output_name),
            tmp_path,
            api_key="test",
            base_url="https://example.invalid",
        )


def test_corrupt_png_is_rejected(tmp_path):
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1024).to_bytes(4, "big") * 2
    provider = OpenAIImagesProvider("test", "https://example.invalid")
    with patch("urllib.request.urlopen", return_value=_Response(fake_png)):
        result = provider.create(ImageRequest(id="bad", prompt="bad"), tmp_path)
    assert result.ok is False
    assert result.error == "invalid_image_bytes"


@pytest.mark.parametrize(
    ("format_name", "expected_format", "expected_suffix"),
    [("PNG", "PNG", ".png"), ("JPEG", "JPEG", ".jpg"), ("WEBP", "WEBP", ".webp")],
)
def test_provider_uses_detected_format_and_dimensions(tmp_path, format_name, expected_format, expected_suffix):
    provider = OpenAIImagesProvider("test", "https://example.invalid")
    with patch("urllib.request.urlopen", return_value=_Response(_image_bytes(format_name))):
        result = provider.create(ImageRequest(id="image", prompt="image", size="7x5"), tmp_path)
    assert result.ok is True
    assert result.format_detected == expected_format
    assert result.actual_size == "7x5"
    assert Path(result.path).suffix == expected_suffix


def test_truncated_image_probe_returns_unknown(tmp_path):
    path = tmp_path / "truncated.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert detect_image(path) == ("UNKNOWN", None, None)


def test_empty_edit_result_preserves_edit_mode(tmp_path):
    result = OpenAIImagesProvider("test", "https://example.invalid").edit(
        ImageRequest(id="edit", prompt="edit", mode="edit"), tmp_path
    )
    assert result.ok is False
    assert result.mode == "edit"
    assert result.error == "missing_input_images"


@pytest.mark.parametrize(
    ("raw", "expected_error"),
    [
        (b"not-json", "provider_json_error"),
        (json.dumps({"data": [{"b64_json": "%%%"}]}).encode(), "invalid_image_bytes"),
    ],
)
def test_provider_content_errors_are_classified_and_retryable(monkeypatch, tmp_path, raw, expected_error):
    responses = [_RawResponse(raw), _Response(_image_bytes("PNG"))]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: responses.pop(0))

    result = generate_images_batch(
        [ImageRequest(id="retry-content", prompt="retry")],
        tmp_path,
        api_key="test",
        base_url="https://example.invalid",
        retry=1,
        qa_enabled=False,
        job_id=f"job-{expected_error}",
    )

    assert result.ok is True
    manifest = json.loads(result.manifest_path.read_text())
    assert [attempt["error"] for attempt in manifest["attempts"]] == [expected_error, None]


@pytest.mark.parametrize(
    ("image_request", "message"),
    [
        (ImageRequest(id="", prompt="ok"), "id"),
        (ImageRequest(id="ok", prompt="   "), "prompt"),
        (ImageRequest(id="ok", prompt="ok", mode="other"), "mode"),
        (ImageRequest(id="ok", prompt="ok", size="bad"), "size"),
        (ImageRequest(id="ok", prompt="ok", metadata={"bad": object()}), "metadata"),
    ],
)
def test_single_validates_request_fields(tmp_path, image_request, message):
    with pytest.raises(ValueError, match=message):
        generate_image(image_request, tmp_path, api_key="test", base_url="https://example.invalid")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"concurrency": 0}, "concurrency"),
        ({"retry": -1}, "retry"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"concurrency": 1.5}, "concurrency"),
        ({"retry": True}, "retry"),
        ({"timeout_seconds": "10"}, "timeout_seconds"),
    ],
)
def test_batch_validates_execution_options(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        generate_images_batch(
            [ImageRequest(id="ok", prompt="ok")],
            tmp_path,
            api_key="test",
            base_url="https://example.invalid",
            **kwargs,
        )
