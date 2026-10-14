"""Dynamic source descriptors persisted through the standard data bus."""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Mapping


class Sources:
    """Register source descriptions without coupling them to fetch methods."""

    identity = "registry"
    filename = "sources.json"

    def __init__(self, data):
        self.data = data
        self._registry = {}
        self._items = {}
        self._refresh()

    def _read_registry(self) -> dict:
        from protocal import DataError

        try:
            value = json.loads(self.data.read("sources", self.identity, self.filename))
        except DataError as error:
            if error.code != "missing":
                raise
            return {"sources": [], "datasets": [], "benchmarks": []}
        if not isinstance(value, dict):
            raise ValueError("source registry must be an object")
        return value

    def _load(self) -> dict[str, dict]:
        value = self._registry
        rows = value.get("sources", []) if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise ValueError("source registry must contain a sources list")
        result = {}
        for row in rows:
            self._validate(row)
            if row["id"] in result:
                raise ValueError("duplicate source identity")
            result[row["id"]] = deepcopy(dict(row))
        return result

    def _refresh(self, registry=None) -> None:
        self._registry = self._read_registry() if registry is None else registry
        self._items = self._load()

    @staticmethod
    def _validate(source: Mapping) -> None:
        if not isinstance(source, Mapping):
            raise TypeError("source descriptor must be an object")
        required = {"id", "name", "method", "frequency", "defaults"}
        if not required <= set(source) or set(source) - required - {
                "category", "data_type", "fields", "storage_id", "dataset_id", "calendar",
                "provider", "price_basis", "columns", "availability", "source_file",
                "code_format", "params", "modes", "default"}:
            raise ValueError("source descriptor fields are invalid")
        identity = source["id"]
        if (not isinstance(identity, str) or not identity
                or not identity.replace("_", "").replace("-", "").isalnum()):
            raise ValueError("invalid source identity")
        for name in ("name", "method", "frequency"):
            if not isinstance(source[name], str) or not source[name].strip():
                raise ValueError(f"source {name} is required")
        if not isinstance(source["defaults"], dict):
            raise TypeError("source defaults must be an object")
        if "modes" in source and (not isinstance(source["modes"], list)
                or not source["modes"] or any(not isinstance(mode, str) or not mode
                                               for mode in source["modes"])):
            raise TypeError("source modes must be a non-empty list")
        if "default" in source and type(source["default"]) is not bool:
            raise TypeError("source default must be boolean")

    def register(self, source: Mapping) -> dict:
        self._validate(source)
        identity = source["id"]
        value = deepcopy(dict(source))
        value["defaults"] = deepcopy(source["defaults"])
        def change(registry):
            rows = registry.setdefault("sources", [])
            if any(row.get("id") == identity for row in rows):
                raise ValueError("source identity already registered")
            rows.append(value)
            return registry
        registry = self.data.update_registry(change)
        self._refresh(registry)
        return self.get(identity)

    def update(self, source_id: str, settings: Mapping) -> dict:
        if not isinstance(settings, Mapping):
            raise TypeError("source settings must be an object")
        def change(registry):
            rows = registry.setdefault("sources", [])
            for row in rows:
                if row.get("id") == source_id:
                    value = deepcopy(row)
                    value["defaults"] = {**value["defaults"], **deepcopy(dict(settings))}
                    self._validate(value)
                    row.clear()
                    row.update(value)
                    return registry
            raise KeyError(source_id)
        registry = self.data.update_registry(change)
        self._refresh(registry)
        return self.get(source_id)

    def remove(self, source_id: str) -> None:
        def change(registry):
            rows = registry.setdefault("sources", [])
            kept = [row for row in rows if row.get("id") != source_id]
            if len(kept) == len(rows):
                raise KeyError(source_id)
            registry["sources"] = kept
            return registry
        registry = self.data.update_registry(change)
        self._refresh(registry)

    def get(self, source_id: str) -> dict:
        self._refresh()
        try:
            value = self._items[source_id]
        except KeyError as error:
            raise KeyError(source_id) from error
        return deepcopy(value)

    def list(self) -> list[dict]:
        self._refresh()
        return [self.get(source_id) for source_id in sorted(self._items)]

    def select(self, *, mode: str, data_type: str) -> dict:
        self._refresh()
        matches = [source for source in self._items.values()
                   if source.get("data_type") == data_type and
                   mode in source.get("modes", ()) and source.get("default") is True]
        if len(matches) != 1:
            raise ValueError(f"expected one default source for {mode}/{data_type}, found {len(matches)}")
        return deepcopy(matches[0])
