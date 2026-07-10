from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError


def detect_image(path: Path) -> tuple[str, int | None, int | None]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            detected_format = image.format
            width, height = image.size
    except (OSError, ValueError, UnidentifiedImageError):
        return "UNKNOWN", None, None
    if detected_format not in {"PNG", "JPEG", "WEBP"}:
        return "UNKNOWN", None, None
    return detected_format, width, height


def expected_ratio(size: str) -> float | None:
    if size == "auto":
        return None
    try:
        width_text, height_text = size.lower().split("x", 1)
        return int(width_text) / int(height_text)
    except (ValueError, ZeroDivisionError):
        return None


def aspect_ratio_ok(actual_width: int | None, actual_height: int | None, requested_size: str, tolerance: float = 0.02) -> bool | None:
    ratio = expected_ratio(requested_size)
    if ratio is None or not actual_width or not actual_height:
        return None
    actual_ratio = actual_width / actual_height
    return abs(actual_ratio - ratio) <= tolerance
