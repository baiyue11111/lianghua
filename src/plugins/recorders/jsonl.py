"""Canonical schema-2 audit log: decision/target may be null, no signal field."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
from typing import Mapping

from core.contracts import StepResult


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


class JsonlRecorder:
    def __init__(self, path: Path, *, run_id: str) -> None:
        self.path = Path(path)
        if not run_id.strip():
            raise ValueError("run_id is required")
        if self.path.exists():
            raise FileExistsError(self.path)
        if not self.path.parent.is_dir():
            raise FileNotFoundError(self.path.parent)
        self.run_id = run_id
        self._sequence = 0

    def record(self, result: StepResult) -> None:
        self._sequence += 1
        row = {
            "schema": 2,
            "run_id": self.run_id,
            "sequence": self._sequence,
            **_json_value(result),
        }
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
