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
from .validation import validate_batch_requests, validate_request, validate_safe_name


def _safe_qa(result: ImageResult, preset: str | None) -> QaResult:
    try:
        return qa_check_image(result, preset=preset)
    except Exception as error:  # QA must never abort the batch.
        return QaResult(status="not_checked", preset=preset, qa_error=f"{type(error).__name__}: {error}")


def _safe_emit(logger: EventLogger, event: str, **payload) -> str | None:
    try:
        logger.emit(event, **payload)
        return None
    except Exception as error:
        return f"{type(error).__name__}: {error}"


def _safe_write_json(path: Path, data) -> str | None:
    try:
        write_json(path, data)
        return None
    except Exception as error:
        return f"{type(error).__name__}: {error}"


def _preflight_output_dir(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise OSError(f"output_dir is not writable: {root} ({type(error).__name__}: {error})") from error


RETRYABLE_ERRORS = {
    "http_error",
    "missing_b64_json",
    "invalid_image_bytes",
    "provider_json_error",
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
    validate_request(request)
    _validate_positive_int(timeout_seconds, "timeout_seconds")
    provider = _provider_for(request, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)
    result = provider.generate(request, Path(output_dir))
    if result.ok and qa_enabled:
        result.qa = _safe_qa(result, request.qa_preset)
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
    validate_batch_requests(requests)
    _validate_positive_int(concurrency, "concurrency")
    _validate_nonnegative_int(retry, "retry")
    _validate_positive_int(timeout_seconds, "timeout_seconds")
    job_id = job_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    validate_safe_name(job_id, "job_id")
    root = Path(output_dir) / job_id
    _preflight_output_dir(root)
    images_dir = root / "images"
    events_path = root / "events.jsonl"
    manifest_path = root / "manifest.json"
    logger = EventLogger(events_path)
    write_errors: list[str] = []
    err = _safe_emit(logger, "batch_started", job_id=job_id, count=len(requests), concurrency=concurrency, retry=retry)
    if err:
        write_errors.append(err)

    pending = list(requests)
    successful: dict[str, ImageResult] = {}
    final_failures: dict[str, ImageResult] = {}
    all_attempts: list[ImageResult] = []

    max_workers = max(1, min(concurrency, len(requests)))
    for attempt in range(1, retry + 2):
        if not pending:
            break
        event_name = "round_started" if attempt == 1 else "retry_started"
        err = _safe_emit(logger, event_name, attempt=attempt, count=len(pending))
        if err:
            write_errors.append(err)
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
                    result.qa = _safe_qa(result, request.qa_preset)
                successful[result.id] = result
                _safe_emit(logger, "image_succeeded", result=result)
                if result.qa.qa_error:
                    _safe_emit(logger, "qa_error", result=result)
                elif result.qa.status in {"warning", "severe_warning"}:
                    _safe_emit(logger, "qa_warning", result=result)
                continue
            _safe_emit(logger, "image_failed", result=result)
            if attempt <= retry and _is_retryable(result):
                pending.append(request)
            else:
                final_failures[result.id] = result
        err = _safe_emit(
            logger,
            "round_completed",
            attempt=attempt,
            success_count=len(successful),
            failed_count=len(final_failures),
            retry_pending_count=len(pending),
        )
        if err:
            write_errors.append(err)
        err = _safe_write_json(manifest_path, _manifest(job_id, requests, successful, final_failures, pending, all_attempts))
        if err:
            write_errors.append(err)
        if attempt == 1:
            err = _safe_emit(logger, "partial_delivery_ready", success_count=len(successful), retry_pending_count=len(pending))
            if err:
                write_errors.append(err)

    ordered_results = []
    for request in requests:
        ordered_results.append(successful.get(request.id) or final_failures.get(request.id) or _pending_result(request))
    err = _safe_emit(logger, "batch_completed", ok=all(result.ok for result in ordered_results), success_count=sum(1 for result in ordered_results if result.ok), write_errors=write_errors or None)
    if err:
        write_errors.append(err)
    err = _safe_write_json(manifest_path, _manifest(job_id, requests, successful, final_failures, [], all_attempts, write_errors))
    if err:
        write_errors.append(err)
    return BatchResult(job_id=job_id, output_dir=root, results=ordered_results, manifest_path=manifest_path, events_path=events_path, write_errors=list(write_errors))


def _validate_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _run_round(
    requests: list[ImageRequest],
    output_dir: Path,
    api_key: str | None,
    base_url: str | None,
    timeout_seconds: int,
    max_workers: int,
    attempt: int,
) -> list[ImageResult]:
    results: list[ImageResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _execute_request,
                request,
                output_dir,
                api_key,
                base_url,
                timeout_seconds,
                attempt,
            ): request
            for request in requests
        }
        for future in concurrent.futures.as_completed(future_map):
            request = future_map[future]
            try:
                results.append(future.result())
            except Exception as error:  # Defensive boundary: one task must never abort its batch.
                results.append(_exception_result(request, attempt, error))
    return results


def _execute_request(
    request: ImageRequest,
    output_dir: Path,
    api_key: str | None,
    base_url: str | None,
    timeout_seconds: int,
    attempt: int,
) -> ImageResult:
    try:
        provider = _provider_for(request, api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)
        return provider.generate(request, output_dir, attempt)
    except ValueError as error:
        if str(error).startswith("unsupported provider"):
            return _exception_result(request, attempt, error, "unsupported_provider")
        raise


def _exception_result(
    request: ImageRequest,
    attempt: int,
    error: Exception,
    error_name: str | None = None,
) -> ImageResult:
    return ImageResult(
        ok=False,
        id=request.id,
        provider=request.provider,
        model=request.model,
        requested_size=request.size,
        mode=request.mode,
        attempt=attempt,
        error=error_name or type(error).__name__,
        provider_error={"message": str(error)},
        metadata=request.metadata,
    )


def _provider_for(request: ImageRequest, api_key: str | None, base_url: str | None, timeout_seconds: int) -> OpenAIImagesProvider:
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
        mode=request.mode,
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
    write_errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "requests": requests,
        "results": [successful.get(request.id) or final_failures.get(request.id) or _pending_result(request) for request in requests],
        "retry_pending_ids": [request.id for request in retry_pending],
        "attempts": all_attempts,
        "write_errors": list(write_errors) if write_errors else [],
    }
