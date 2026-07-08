from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def dataclass_to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {key: dataclass_to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [dataclass_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: dataclass_to_dict(item) for key, item in value.items()}
    return value


class EventLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **payload: Any) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(dataclass_to_dict(record), ensure_ascii=False) + "\n")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclass_to_dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
