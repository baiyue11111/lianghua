"""The single active data gateway for the project."""

from __future__ import annotations

from hashlib import sha256
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING
from uuid import uuid4


if TYPE_CHECKING:
    from protocal import DataRef


PHYSICAL_KINDS = {
    "sources": "source",
    "runs": "result",
    "state": "log",
    "validation": "result",
}


class DataStore:
    """Store every active datum below one project-local ``data`` directory."""

    def __init__(self, project: Path) -> None:
        self.project = Path(project).resolve()
        self.root = self.project / "data"
        self._locks = threading.local()

    def _publish_path(self, dataset: str) -> Path:
        if not dataset or Path(dataset).name != dataset:
            raise ValueError("invalid dataset identity")
        return self.root / "source" / ".publish" / f"{dataset}.state"

    @contextmanager
    def source_lock(self):
        """Serialize the complete source range across threads and processes."""
        lock_path = self.root / "source" / ".locks" / "source.lock"
        locks = getattr(self._locks, "values", {})
        current = locks.get("source")
        if current is not None:
            locks["source"] = current + 1
            try:
                yield
            finally:
                locks["source"] -= 1
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locks["source"] = 1
        self._locks.values = locks
        try:
            yield
        finally:
            locks.pop("source", None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def publish_state(self, dataset: str) -> str | None:
        path = self._publish_path(dataset)
        try:
            return path.read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            return "<invalid-publish-state>"

    def publish_states(self) -> tuple[str, ...]:
        folder = self.root / "source" / ".publish"
        if not folder.exists():
            return ()
        states = []
        for path in sorted(folder.glob("*.state")):
            try:
                states.append(path.read_text())
            except (OSError, UnicodeError):
                states.append("<invalid-publish-state>")
        return tuple(states)

    def begin_publish(self, dataset: str, token: str) -> None:
        path = self._publish_path(dataset)
        if path.exists():
            raise ValueError("dataset publication is already in progress")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(token)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def finish_publish(self, dataset: str, token: str) -> None:
        path = self._publish_path(dataset)
        if self.publish_state(dataset) != token:
            raise ValueError("dataset publication token changed")
        path.unlink(missing_ok=True)

    def path(self, kind: str, identity: str, name: str) -> Path:
        if (
            any(not value or value in (".", "..") or Path(value).name != value for value in (kind, identity))
            or (kind == "sources" and identity.startswith("."))
        ):
            raise ValueError("invalid data identity")
        physical_kind = PHYSICAL_KINDS.get(kind)
        if physical_kind is None:
            raise ValueError("invalid data kind")
        relative = Path(name)
        if not name or not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid data file")
        base = self.project / "log" if physical_kind == "log" else self.root / physical_kind
        identity_root = base / identity
        if identity_root.is_symlink():
            raise ValueError("data identity must be a real directory")
        target = (identity_root / relative).resolve()
        if not target.is_relative_to(base) or not target.is_relative_to(identity_root.resolve()):
            raise ValueError("data path escapes identity")
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
            if kind == "sources" and identity is None and relative.parts[0].startswith("."):
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
