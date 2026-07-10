from __future__ import annotations

import json
import re

from .models import ImageRequest


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_safe_name(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{field_name} must be a safe filename component")


def validate_request(request: ImageRequest) -> None:
    validate_safe_name(request.id, "id")
    if not isinstance(request.prompt, str) or not request.prompt.strip():
        raise ValueError("prompt must not be empty")
    if request.mode not in {"generate", "edit"}:
        raise ValueError("mode must be 'generate' or 'edit'")
    if request.output_name is not None:
        validate_safe_name(request.output_name, "output_name")
    if not isinstance(request.input_images, list) or not all(isinstance(item, str) and item for item in request.input_images):
        raise ValueError("input_images must contain non-empty paths")
    if request.size != "auto" and (
        not isinstance(request.size, str) or re.fullmatch(r"[1-9]\d*x[1-9]\d*", request.size) is None
    ):
        raise ValueError("size must be 'auto' or WIDTHxHEIGHT with positive integers")
    try:
        json.dumps(request.metadata)
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be JSON serializable") from error


def validate_batch_requests(requests: list[ImageRequest]) -> None:
    if not requests:
        raise ValueError("requests must not be empty")
    for request in requests:
        validate_request(request)
    ids = [request.id for request in requests]
    if len(ids) != len(set(ids)):
        raise ValueError("request ids must be unique")
