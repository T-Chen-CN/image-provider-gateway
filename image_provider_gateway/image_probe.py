from __future__ import annotations

import struct
from pathlib import Path


def detect_image(path: Path) -> tuple[str, int | None, int | None]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", data[16:24])
        return "PNG", width, height
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in (0xD8, 0xD9):
                continue
            length = struct.unpack(">H", data[index:index + 2])[0]
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            }:
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return "JPEG", width, height
            index += length
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP", None, None
    return "UNKNOWN", None, None


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
