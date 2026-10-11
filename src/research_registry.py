"""Immutable research receipts and derived daily counters. No strategy logic."""
from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("SHA-256 required")


class ResearchRegistry:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _save(self, kind, payload):
        key = digest(payload)
        directory = self.directory / kind
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{key}.json"
        content = canonical({"schema": 1, "record_id": key, "payload": payload})
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != content:
                    raise ValueError("immutable record collision or corruption")
        finally:
            temporary.unlink()
        return key

    def _read(self, kind):
        records = []
        for path in sorted((self.directory / kind).glob("*.json")):
            record = json.loads(path.read_bytes())
            if (record.get("schema") != 1 or "payload" not in record
                    or record.get("record_id") != digest(record["payload"])
                    or path.stem != record["record_id"]):
                raise ValueError(f"research receipt failed integrity check: {path}")
            records.append(record)
        return records

    def record_run(self, payload):
        required = {"strategy_id", "family_id", "config_hash", "input_hash", "code_hash",
                    "completed_on", "status", "data_verified", "accounting_verified",
                    "historical_qualified", "metrics"}
        if not required <= payload.keys():
            raise ValueError("research run fields missing")
        date.fromisoformat(payload["completed_on"])
        for name in ("config_hash", "input_hash", "code_hash"):
            _hash(payload[name])
        if payload["status"] not in {"completed", "failed", "invalid"}:
            raise ValueError("invalid research run status")
        for name in ("data_verified", "accounting_verified", "historical_qualified"):
            if type(payload[name]) is not bool:
                raise ValueError(f"{name} must be boolean")
        if not all(isinstance(payload[n], str) and payload[n].strip() for n in ("strategy_id", "family_id")):
            raise ValueError("strategy and family identities required")
        if payload["historical_qualified"]:
            if not self._valid(payload):
                raise ValueError("invalid input/accounting cannot qualify")
            _hash(payload.get("validation_artifact_hash"))
        return self._save("runs", payload)

    @staticmethod
    def _valid(run):
        return run["status"] == "completed" and run["data_verified"] and run["accounting_verified"]

    def record_states(self, run_id, policy_hash, shadow_input_hash, events, *, gate_verified):
        _hash(policy_hash)
        _hash(shadow_input_hash)
        runs = {r["record_id"]: r["payload"] for r in self._read("runs")}
        if run_id not in runs or not self._valid(runs[run_id]):
            raise ValueError("states require a valid completed run")
        if type(gate_verified) is not bool:
            raise ValueError("gate_verified must be boolean")
        rows = [{**asdict(e), "day": e.day.isoformat()} for e in events]
        if any(a["day"] >= b["day"] for a, b in zip(rows, rows[1:])):
            raise ValueError("state dates must increase")
        run = runs[run_id]
        if any(e["state"] == "active" and (not run["historical_qualified"] or e["day"] < run["completed_on"]) for e in rows):
            raise ValueError("active state cannot precede historical qualification")
        return self._save("states", {"run_id": run_id, "policy_hash": policy_hash,
                                    "shadow_input_hash": shadow_input_hash,
                                    "gate_verified": gate_verified, "events": rows})

    def dashboard(self, as_of: str):
        date.fromisoformat(as_of)
        runs = self._read("runs")
        today = [r["payload"] for r in runs if r["payload"]["completed_on"] == as_of]
        valid_today = [r for r in today if self._valid(r)]
        states = self._read("states")
        latest = {}
        by_id = {r["record_id"]: r["payload"] for r in runs}
        for record in states:
            payload = record["payload"]
            if payload["run_id"] not in by_id:
                raise ValueError("orphan state receipt")
            # An unvalidated selector is a research experiment, not activation.
            if not payload["gate_verified"]:
                continue
            run = by_id[payload["run_id"]]
            key = (run["strategy_id"], run["config_hash"], payload["policy_hash"])
            for event in payload["events"]:
                if event["day"] > as_of:
                    continue
                row = {**event, "strategy_id": run["strategy_id"], "run_id": payload["run_id"],
                       "config_hash": run["config_hash"], "policy_hash": payload["policy_hash"],
                       "metrics": run["metrics"]}
                if key in latest and event["day"] == latest[key]["day"] and row != latest[key]:
                    raise ValueError("conflicting strategy states on the same day")
                if key not in latest or event["day"] > latest[key]["day"]:
                    latest[key] = row
        rows = sorted(({**row, "assessment_current": row["day"] == as_of}
                       for row in latest.values()),
                      key=lambda r: (r["state"] != "active", r["strategy_id"], r["config_hash"]))
        return {"as_of": as_of, "today_completed": len({(r["strategy_id"], r["config_hash"]) for r in valid_today}),
                "today_runs": len(valid_today), "today_families": len({r["family_id"] for r in valid_today}),
                "today_failed": sum(r["status"] == "failed" for r in today),
                "today_invalid": sum(r["status"] != "failed" and not self._valid(r) for r in today),
                "active": sum(r["state"] == "active" and r["assessment_current"] for r in rows),
                "today_paused": sum(r["day"] == as_of and r["transition"] == "paused" for r in rows),
                "today_resumed": sum(r["day"] == as_of and r["transition"] == "resumed" for r in rows),
                "strategies": rows}
