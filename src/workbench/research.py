"""Canonical research runs stored through the single project data port."""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort


class Research:
    def __init__(self, data: DataPort) -> None:
        self.data = data

    def _ids(self) -> tuple[str, ...]:
        names: dict[str, set[str]] = {}
        for ref in self.data.list("runs"):
            names.setdefault(ref.identity, set()).add(ref.name)
        return tuple(sorted(identity for identity, files in names.items()
                            if {"manifest.json", "metrics.json", "result.json", "trace.json"} <= files))

    def _read(self, identity: str) -> dict:
        manifest_bytes = self.data.read("runs", identity, "manifest.json")
        metrics_bytes = self.data.read("runs", identity, "metrics.json")
        result_bytes = self.data.read("runs", identity, "result.json")
        manifest = json.loads(manifest_bytes)
        metrics = json.loads(metrics_bytes)
        result = json.loads(result_bytes)
        return {
            "id": identity,
            "run_id": manifest["run_id"],
            "strategy_id": manifest["strategy_id"],
            "status": manifest["status"],
            "qualification": "unverified",
            "limitations": manifest.get("limitations", []),
            "metrics": metrics,
            "manifest_file": f"data/runs/{identity}/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "metrics_sha256": hashlib.sha256(metrics_bytes).hexdigest(),
            "source_hashes": manifest.get("source_hashes", {}),
            "config_hash": manifest.get("config_hash"),
            "note": manifest.get("note", "运行结果未经历史资格认定。"),
            "parameters": manifest.get("parameters", {}),
            "effective_config": manifest.get("effective_config", {}),
            "code_hash": manifest.get("code_hash"),
            "data_hash": manifest.get("data_hash"),
            "execution_hash": manifest.get("execution_hash"),
            "result": result,
        }

    def list(self) -> list[dict]:
        return [self._read(identity) for identity in self._ids()]

    def detail(self, identity: str, offset: int = 0, limit: int = 50) -> dict:
        if identity not in self._ids():
            raise KeyError(identity)
        value = self._read(identity)
        manifest = json.loads(self.data.read("runs", identity, "manifest.json"))
        refs = {(item.identity, item.name): item for item in self.data.list("runs")}
        result = value.pop("result")
        all_trades = result.get("trades", [])
        all_equity = result.get("equity", [])
        trades = all_trades[offset:offset + limit]
        stride = max(1, (len(all_equity) + 799) // 800)
        equity = all_equity[::stride]
        if all_equity and equity[-1] != all_equity[-1]:
            equity.append(all_equity[-1])
        value["trades_total"] = len(all_trades)
        value["equity_sampling"] = {"total": len(all_equity), "stride": stride}
        evidence = []
        for name in ("manifest.json", "metrics.json", "result.json", "trace.json"):
            ref = refs.get((identity, name))
            if ref is not None:
                evidence.append({"file": ref.file, "sha256": ref.sha256})
        return {
            **value,
            "manifest": manifest,
            "trades": trades,
            "trades_total": value.get("trades_total", 0),
            "equity": equity,
            "evidence": evidence,
            "offset": offset,
            "limit": limit,
        }
