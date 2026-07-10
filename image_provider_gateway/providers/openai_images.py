from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ..image_probe import aspect_ratio_ok, detect_image
from ..models import ImageRequest, ImageResult


FORMAT_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


class OpenAIImagesProvider:
    name = "openai_images"

    def __init__(self, api_key: str, base_url: str, timeout_seconds: int = 600):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate(self, request: ImageRequest, output_dir: Path, attempt: int = 1) -> ImageResult:
        if request.mode == "edit" or request.input_images:
            return self.edit(request, output_dir, attempt=attempt)
        return self.create(request, output_dir, attempt=attempt)

    def create(self, request: ImageRequest, output_dir: Path, attempt: int = 1) -> ImageResult:
        payload = {
            "model": request.model,
            "prompt": request.prompt,
            "n": 1,
            "size": request.size,
            "quality": request.quality,
        }
        return self._send_json("/images/generations", payload, request, output_dir, attempt)

    def edit(self, request: ImageRequest, output_dir: Path, attempt: int = 1) -> ImageResult:
        if not request.input_images:
            return ImageResult(
                ok=False,
                id=request.id,
                provider=self.name,
                model=request.model,
                requested_size=request.size,
                mode=request.mode,
                attempt=attempt,
                error="missing_input_images",
                metadata=request.metadata,
            )
        fields = {
            "model": request.model,
            "prompt": request.prompt,
            "n": "1",
            "size": request.size,
        }
        if request.quality:
            fields["quality"] = request.quality
        files = []
        try:
            for image_path in request.input_images:
                path = Path(image_path)
                files.append(("image", path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
        except OSError as error:
            return ImageResult(
                ok=False,
                id=request.id,
                provider=self.name,
                model=request.model,
                requested_size=request.size,
                mode=request.mode,
                attempt=attempt,
                error=type(error).__name__,
                provider_error={"message": str(error)},
                metadata=request.metadata,
            )
        return self._send_multipart("/images/edits", fields, files, request, output_dir, attempt)

    def _send_json(self, endpoint: str, payload: dict[str, Any], request: ImageRequest, output_dir: Path, attempt: int) -> ImageResult:
        http_request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._send(http_request, request, output_dir, attempt)

    def _send_multipart(
        self,
        endpoint: str,
        fields: dict[str, str],
        files: list[tuple[str, str, bytes, str]],
        request: ImageRequest,
        output_dir: Path,
        attempt: int,
    ) -> ImageResult:
        if not files:
            return ImageResult(
                ok=False,
                id=request.id,
                provider=self.name,
                model=request.model,
                requested_size=request.size,
                mode=request.mode,
                attempt=attempt,
                error="missing_input_images",
                metadata=request.metadata,
            )
        boundary = f"----image-provider-gateway-{uuid.uuid4().hex}"
        body = _multipart_body(boundary, fields, files)
        http_request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        return self._send(http_request, request, output_dir, attempt)

    def _send(self, http_request: urllib.request.Request, request: ImageRequest, output_dir: Path, attempt: int) -> ImageResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        result = ImageResult(
            ok=False,
            id=request.id,
            provider=self.name,
            model=request.model,
            requested_size=request.size,
            mode=request.mode,
            attempt=attempt,
            metadata=request.metadata,
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", "replace")
                result.http_status = response.status
                result.elapsed_seconds = round(time.time() - started_at, 2)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    result.error = "provider_json_error"
                    result.provider_error = {"raw_preview": raw[:2000]}
                    return result
                first = (data.get("data") or [{}])[0]
                b64_json = first.get("b64_json") or data.get("b64_json")
                result.usage = data.get("usage") or first.get("usage")
                if not b64_json:
                    result.error = "missing_b64_json"
                    result.provider_error = {"raw_preview": raw[:2000]}
                    return result
                try:
                    image_bytes = base64.b64decode(b64_json, validate=True)
                except (binascii.Error, ValueError, TypeError) as error:
                    result.error = "invalid_image_bytes"
                    result.provider_error = {"message": str(error)}
                    return result
                output_name = request.output_name or request.id
                temporary_path = output_dir / f".{output_name}.{uuid.uuid4().hex}.tmp"
                temporary_path.write_bytes(image_bytes)
                detected_format, width, height = detect_image(temporary_path)
                result.ok = detected_format != "UNKNOWN" and width is not None and height is not None
                result.format_detected = detected_format
                result.actual_size = f"{width}x{height}" if width and height else None
                result.aspect_ratio_ok = aspect_ratio_ok(width, height, request.size)
                if not result.ok:
                    temporary_path.unlink(missing_ok=True)
                    result.error = "invalid_image_bytes"
                    return result
                output_path = output_dir / f"{output_name}{FORMAT_SUFFIXES[detected_format]}"
                temporary_path.replace(output_path)
                result.path = str(output_path)
                return result
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            result.http_status = error.code
            result.elapsed_seconds = round(time.time() - started_at, 2)
            result.error = "http_error"
            result.provider_error = _parse_provider_error(body)
            return result
        except TimeoutError as error:
            result.elapsed_seconds = round(time.time() - started_at, 2)
            result.error = "timeout"
            result.provider_error = {"message": repr(error)}
            return result
        except Exception as error:  # noqa: BLE001 - Provider boundary must capture all task-level failures.
            result.elapsed_seconds = round(time.time() - started_at, 2)
            result.error = type(error).__name__
            result.provider_error = {"message": repr(error)}
            return result


def _multipart_body(boundary: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    for field_name, filename, data, content_type in files:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def _parse_provider_error(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"message": body[:2000]}
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return error
    return {"raw": data}
