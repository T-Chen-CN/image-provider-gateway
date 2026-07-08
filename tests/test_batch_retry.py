from __future__ import annotations

from pathlib import Path

from image_provider_gateway.gateway import generate_images_batch
from image_provider_gateway.models import ImageRequest, ImageResult


class FakeProvider:
    calls: dict[str, int] = {}

    def __init__(self, api_key: str, base_url: str, timeout_seconds: int):
        pass

    def generate(self, request: ImageRequest, output_dir: Path, attempt: int = 1) -> ImageResult:
        self.calls[request.id] = self.calls.get(request.id, 0) + 1
        if request.mode == "edit":
            assert request.input_images
        if request.id == "fail-once" and attempt == 1:
            return ImageResult(
                ok=False,
                id=request.id,
                provider=request.provider,
                model=request.model,
                requested_size=request.size,
                attempt=attempt,
                http_status=500,
                error="http_error",
                provider_error={"message": "fake transient failure"},
            )
        output_path = output_dir / f"{request.output_name or request.id}.png"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        return ImageResult(
            ok=True,
            id=request.id,
            provider=request.provider,
            model=request.model,
            requested_size=request.size,
            path=str(output_path),
            actual_size="1024x1024",
            aspect_ratio_ok=True,
            format_detected="PNG",
            attempt=attempt,
        )


def test_batch_retries_only_failed(monkeypatch, tmp_path):
    FakeProvider.calls = {}
    monkeypatch.setattr("image_provider_gateway.gateway.OpenAIImagesProvider", FakeProvider)
    requests = [
        ImageRequest(id="success", prompt="ok"),
        ImageRequest(id="edit-success", prompt="edit", mode="edit", input_images=["/tmp/reference.png"]),
        ImageRequest(id="fail-once", prompt="retry"),
    ]
    result = generate_images_batch(
        requests,
        tmp_path,
        api_key="fake",
        concurrency=2,
        retry=1,
        qa_enabled=False,
        job_id="test-job",
    )
    assert result.ok
    assert FakeProvider.calls == {"success": 1, "edit-success": 1, "fail-once": 2}
    assert result.manifest_path.exists()
    assert result.events_path.exists()
