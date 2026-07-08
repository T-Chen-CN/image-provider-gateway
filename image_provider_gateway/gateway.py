from __future__ import annotations

import concurrent.futures
import os
import time
import uuid
from pathlib import Path

from .manifest import EventLogger, write_json
from .models import BatchResult, ImageRequest, ImageResult, QaResult
from .providers.openai_images import OpenAIImagesProvider
from .qa_check import qa_check_image


RETRYABLE_ERRORS = {
    "http_error",
    "missing_b64_json",
    "invalid_image_bytes",
    "timeout",
    "TimeoutError",
    "URLError",
}


def generate_image(
    request: ImageRequest,
    output_dir: Path | str,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 600,
    qa_enabled: bool = False,
) -> ImageResult:
    provider = _provider_for(request, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)
    result = provider.generate(request, Path(output_dir))
    if result.ok and qa_enabled:
        result.qa = qa_check_image(result, preset=request.qa_preset)
    return result


def generate_images_batch(
    requests: list[ImageRequest],
    output_dir: Path | str,
    api_key: str | None = None,
    base_url: str | None = None,
    concurrency: int = 9,
    retry: int = 2,
    timeout_seconds: int = 600,
    qa_enabled: bool = True,
    job_id: str | None = None,
) -> BatchResult:
    if not requests:
        raise ValueError("requests must not be empty")
    job_id = job_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    root = Path(output_dir) / job_id
    images_dir = root / "images"
    events_path = root / "events.jsonl"
    manifest_path = root / "manifest.json"
    logger = EventLogger(events_path)
    logger.emit("batch_started", job_id=job_id, count=len(requests), concurrency=concurrency, retry=retry)

    pending = list(requests)
    successful: dict[str, ImageResult] = {}
    final_failures: dict[str, ImageResult] = {}
    all_attempts: list[ImageResult] = []

    max_workers = max(1, min(concurrency, len(requests)))
    for attempt in range(1, retry + 2):
        if not pending:
            break
        event_name = "round_started" if attempt == 1 else "retry_started"
        logger.emit(event_name, attempt=attempt, count=len(pending))
        round_results = _run_round(
            pending,
            images_dir,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_workers=max_workers if attempt == 1 else min(max_workers, len(pending)),
            attempt=attempt,
        )
        all_attempts.extend(round_results)
        pending = []
        for result in round_results:
            request = next(item for item in requests if item.id == result.id)
            if result.ok:
                if qa_enabled:
                    result.qa = qa_check_image(result, preset=request.qa_preset)
                successful[result.id] = result
                logger.emit("image_succeeded", result=result)
                if result.qa.status in {"warning", "severe_warning"}:
                    logger.emit("qa_warning", result=result)
                continue
            logger.emit("image_failed", result=result)
            if attempt <= retry and _is_retryable(result):
                pending.append(request)
            else:
                final_failures[result.id] = result
        logger.emit(
            "round_completed",
            attempt=attempt,
            success_count=len(successful),
            failed_count=len(final_failures),
            retry_pending_count=len(pending),
        )
        write_json(manifest_path, _manifest(job_id, requests, successful, final_failures, pending, all_attempts))
        if attempt == 1:
            logger.emit("partial_delivery_ready", success_count=len(successful), retry_pending_count=len(pending))

    ordered_results = []
    for request in requests:
        ordered_results.append(successful.get(request.id) or final_failures.get(request.id) or _pending_result(request))
    logger.emit("batch_completed", ok=all(result.ok for result in ordered_results), success_count=sum(1 for result in ordered_results if result.ok))
    write_json(manifest_path, _manifest(job_id, requests, successful, final_failures, [], all_attempts))
    return BatchResult(job_id=job_id, output_dir=root, results=ordered_results, manifest_path=manifest_path, events_path=events_path)


def _run_round(
    requests: list[ImageRequest],
    output_dir: Path,
    api_key: str | None,
    base_url: str,
    timeout_seconds: int,
    max_workers: int,
    attempt: int,
) -> list[ImageResult]:
    results: list[ImageResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _provider_for(request, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds).generate,
                request,
                output_dir,
                attempt,
            ): request
            for request in requests
        }
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())
    return results


def _provider_for(request: ImageRequest, api_key: str | None, base_url: str, timeout_seconds: int) -> OpenAIImagesProvider:
    if request.provider != "openai_images":
        raise ValueError(f"unsupported provider: {request.provider}")
    key = api_key or os.environ.get("IMAGE_API_KEY")
    if not key:
        raise ValueError("IMAGE_API_KEY is required")
    resolved_base_url = base_url or os.environ.get("IMAGE_API_BASE_URL")
    if not resolved_base_url:
        raise ValueError("IMAGE_API_BASE_URL is required")
    return OpenAIImagesProvider(api_key=key, base_url=resolved_base_url, timeout_seconds=timeout_seconds)


def _is_retryable(result: ImageResult) -> bool:
    if result.http_status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    return result.error in RETRYABLE_ERRORS


def _pending_result(request: ImageRequest) -> ImageResult:
    return ImageResult(
        ok=False,
        id=request.id,
        provider=request.provider,
        model=request.model,
        requested_size=request.size,
        error="pending_or_interrupted",
        qa=QaResult(status="not_checked"),
    )


def _manifest(
    job_id: str,
    requests: list[ImageRequest],
    successful: dict[str, ImageResult],
    final_failures: dict[str, ImageResult],
    retry_pending: list[ImageRequest],
    all_attempts: list[ImageResult],
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "requests": requests,
        "results": [successful.get(request.id) or final_failures.get(request.id) or _pending_result(request) for request in requests],
        "retry_pending_ids": [request.id for request in retry_pending],
        "attempts": all_attempts,
    }
