"""Register real research receipts and replay daily states from saved evidence.

No data fetching or strategy execution happens here. Inputs are produced by
the backtest/accounting validation pipeline. Unvalidated gate experiments
are retained for audit but never counted as enabled strategies.
"""
import argparse
import csv
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path

from dynamic_selection import GatePolicy, ShadowReturn, replay_states
from research_registry import ResearchRegistry, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--run", type=Path, help="completed research run receipt payload JSON")
    parser.add_argument("--shadow", type=Path, help="CSV: day,net_return; independent net shadow ledger")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if bool(args.shadow) != bool(args.policy) or (args.shadow and not args.run):
        parser.error("--shadow and --policy require each other and --run")
    store = ResearchRegistry(args.registry)
    if args.run:
        run = json.loads(args.run.read_bytes())
        if date.fromisoformat(run["completed_on"]) > args.as_of:
            parser.error("run completion is after --as-of")
        events, policy_doc, shadow_bytes = None, None, None
        if args.shadow:
            policy_doc = json.loads(args.policy.read_bytes())
            if policy_doc.get("schema") != 1 or policy_doc.get("validation_status") not in {
                "UNVALIDATED_RESEARCH_POLICY", "VALIDATED"
            }:
                raise ValueError("invalid gate policy receipt")
            if policy_doc["validation_status"] == "VALIDATED":
                certificate = Path(policy_doc["validation_artifact"])
                if hashlib.sha256(certificate.read_bytes()).hexdigest() != policy_doc["validation_artifact_hash"]:
                    raise ValueError("gate validation certificate hash mismatch")
            shadow_bytes = args.shadow.read_bytes()
            rows = csv.DictReader(shadow_bytes.decode("utf-8").splitlines())
            points = tuple(ShadowReturn(date.fromisoformat(r["day"]), float(r["net_return"])) for r in rows)
            # Keep the saved full source hash, while decisions use the prefix.
            points = tuple(p for p in points if p.day <= args.as_of)
            policy = GatePolicy(**policy_doc["policy"])
            qualified_on = date.fromisoformat(run["completed_on"]) if run["historical_qualified"] else None
            events = replay_states(points, policy, qualified_on=qualified_on)
        run_id = store.record_run(run)
        if events is not None:
            state_id = store.record_states(
                run_id, digest(policy_doc), hashlib.sha256(shadow_bytes).hexdigest(), events,
                gate_verified=policy_doc["validation_status"] == "VALIDATED")
            print(json.dumps({"run_id": run_id, "state_receipt": state_id,
                              "last_research_state": {**asdict(events[-1]), "day": events[-1].day.isoformat()} if events else None}, ensure_ascii=False))
    print(json.dumps(store.dashboard(args.as_of.isoformat()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
