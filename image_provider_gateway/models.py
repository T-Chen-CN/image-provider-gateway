from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ImageRequest:
    id: str
    prompt: str
    size: str = "1024x1024"
    quality: str = "low"
    provider: str = "openai_images"
    model: str = "gpt-image-2"
    mode: str = "generate"
    input_images: list[str] = field(default_factory=list)
    qa_preset: str = "basic"
    output_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QaIssue:
    severity: str
    category: str
    message: str
    suggestion: str | None = None


@dataclass(slots=True)
class QaResult:
    status: str = "not_checked"
    issues: list[QaIssue] = field(default_factory=list)
    note: str | None = None
    preset: str | None = None
    qa_error: str | None = None


@dataclass(slots=True)
class ImageResult:
    ok: bool
    id: str
    provider: str
    model: str
    requested_size: str
    mode: str = "generate"
    path: str | None = None
    actual_size: str | None = None
    aspect_ratio_ok: bool | None = None
    format_detected: str | None = None
    usage: dict[str, Any] | None = None
    elapsed_seconds: float | None = None
    attempt: int = 1
    http_status: int | None = None
    provider_error: dict[str, Any] | None = None
    error: str | None = None
    qa: QaResult = field(default_factory=QaResult)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchResult:
    job_id: str
    output_dir: Path
    results: list[ImageResult]
    manifest_path: Path
    events_path: Path
    write_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results) and not self.write_errors
