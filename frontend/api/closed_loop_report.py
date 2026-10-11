"""Read immutable schema-1 SignalSet engineering archives only.

New schema-2 decision logs belong to the parameter-run audit path. This reader
does not translate, infer, or fall back between the two log contracts.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


def load_closed_loop_report(manifest_path: Path) -> dict:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require_manifest(manifest)
    events_path = manifest_path.parent / manifest["events_file"]
    payload = events_path.read_bytes()
    actual_hash = sha256(payload).hexdigest()
    if actual_hash != manifest["events_sha256"]:
        raise ValueError("closed-loop events hash mismatch")

    rows = tuple(json.loads(line) for line in payload.decode("utf-8").splitlines())
    if not rows:
        raise ValueError("closed-loop events are empty")
    if any("schema" in row for row in rows):
        raise ValueError("schema-1 archive reader does not accept schema-2 decision logs")
    expected_sequences = list(range(1, len(rows) + 1))
    if [row.get("sequence") for row in rows] != expected_sequences:
        raise ValueError("closed-loop event sequence is not contiguous")
    if any(row.get("run_id") != manifest["run_id"] for row in rows):
        raise ValueError("closed-loop run identity mismatch")

    timeline = tuple(_timeline_row(row) for row in rows)
    trades = tuple(
        _trade(row["sequence"], fill)
        for row in rows
        for fill in row["execution"]["fills"]
    )
    return {
        "artifact_kind": manifest["artifact_kind"],
        "validation": manifest["validation"],
        "strategy": manifest["strategy"],
        "run": {
            "run_id": manifest["run_id"],
            "initial_cash": manifest["initial_cash"],
            "steps": len(rows),
            "first_at": rows[0]["frame"]["at"],
            "last_at": rows[-1]["frame"]["at"],
            "events_sha256": actual_hash,
            "events_file": str(events_path),
        },
        "timeline": timeline,
        "trades": trades,
    }


def _require_manifest(manifest: object) -> None:
    if not isinstance(manifest, dict):
        raise TypeError("closed-loop manifest must be an object")
    if manifest.get("schema") != 1:
        raise ValueError("unsupported closed-loop manifest schema")
    required = (
        "run_id", "artifact_kind", "events_file", "events_sha256",
        "initial_cash", "strategy", "validation",
    )
    missing = tuple(key for key in required if key not in manifest)
    if missing:
        raise ValueError(f"closed-loop manifest missing: {', '.join(missing)}")
    if manifest["artifact_kind"] != "ENGINEERING_GOLDEN":
        raise ValueError("closed-loop artifact kind is not engineering golden")


def _timeline_row(row: dict) -> dict:
    execution = row["execution"]
    account = row["account"]
    planned = row["planned_orders"]
    return {
        "sequence": row["sequence"],
        "at": row["frame"]["at"],
        "breadth_5d": row["frame"]["context"]["breadth_5d"],
        "selection_day": row["frame"]["context"]["selection_day"],
        "signal_count": len(row["signal"]["scores"]),
        "planned_count": len(planned),
        "fill_count": len(execution["fills"]),
        "rejection_count": len(execution["rejections"]),
        "pending_count": len(execution["remaining"]),
        "cash": account["cash"],
        "equity": account["equity"],
        "position_count": len(account["positions"]),
        "positions": account["positions"],
        "action": _action(execution["fills"], planned),
    }


def _action(fills: list[dict], planned: list[dict]) -> str:
    parts: list[str] = []
    for items, suffix in ((fills, "成交"), (planned, "订单")):
        for side, label in (("buy", "买入"), ("sell", "卖出")):
            count = sum(item["side"] == side for item in items)
            if count:
                prefix = "生成" if suffix == "订单" else ""
                parts.append(f"{prefix}{count}笔{label}{suffix}")
    return "；".join(parts) if parts else "观察，不交易"


def _trade(sequence: int, fill: dict) -> dict:
    fees = fill["fee_components"]
    return {
        "sequence": sequence,
        "order_id": fill["order_id"],
        "at": fill["at"],
        "instrument": fill["instrument"],
        "side": fill["side"],
        "quantity": fill["quantity"],
        "price": fill["price"],
        "fee": fill["fee"],
        "commission": fees["commission"],
        "stamp_duty": fees["stamp_duty"],
        "transfer_fee": fees["transfer_fee"],
    }
