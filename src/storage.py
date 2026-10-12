"""The single active data gateway for the project."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4


if TYPE_CHECKING:
    from protocal import DataRef


class DataStore:
    """Store every active datum below one project-local ``data`` directory."""

    def __init__(self, project: Path) -> None:
        self.project = Path(project).resolve()
        self.root = self.project / "data"

    def path(self, kind: str, identity: str, name: str) -> Path:
        if any(not value or value in (".", "..") or Path(value).name != value for value in (kind, identity)):
            raise ValueError("invalid data identity")
        relative = Path(name)
        if not name or not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid data file")
        target = (self.root / kind / identity / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("data path escapes root")
        return target

    def read(self, kind: str, identity: str, name: str) -> bytes:
        return self.path(kind, identity, name).read_bytes()

    def delete(self, kind: str, identity: str, name: str) -> None:
        self.path(kind, identity, name).unlink()

    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef:
        if not isinstance(value, bytes):
            raise TypeError("data value must be bytes")
        target = self.path(kind, identity, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self._ref(kind, identity, name, target)

    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]:
        anchor = self.path(kind, "_", "_").parents[1]
        if identity is not None:
            anchor = self.path(kind, identity, '_').parent
        if not anchor.exists():
            return ()
        refs = []
        for path in sorted(item for item in anchor.rglob("*") if item.is_file()):
            relative = path.relative_to(anchor)
            if identity is None and len(relative.parts) < 2:
                continue
            key = relative.parts[0] if identity is None else identity
            name = Path(*relative.parts[1:]).as_posix() if identity is None else relative.as_posix()
            refs.append(self._ref(kind, key, name, self.path(kind, key, name)))
        return tuple(refs)

    def _ref(self, kind: str, identity: str, name: str, path: Path) -> DataRef:
        from protocal import DataRef
        with path.open('rb') as stream:
            digest = sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return DataRef(
            kind=kind,
            identity=identity,
            name=name,
            file=path.relative_to(self.project).as_posix(),
            sha256=digest.hexdigest(),
            size=size,
        )
