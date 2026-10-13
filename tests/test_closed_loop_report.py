from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from closed_loop_report import load_closed_loop_report


class ClosedLoopReportTests(unittest.TestCase):
    def test_manifest_and_events_become_plain_language_audit_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "events.jsonl"
            rows = [
                {
                    "run_id": "run-1", "sequence": 1,
                    "frame": {"at": "2026-09-28T07:00:00+00:00",
                              "context": {"breadth_5d": 0.61, "selection_day": True}},
                    "signal": {"scores": {"000001": 0.0}},
                    "planned_orders": [{"instrument": "000001", "side": "buy", "quantity": "1200"}],
                    "execution": {"fills": [], "rejections": [], "remaining": []},
                    "account": {"cash": "100000", "equity": "100000", "positions": {}},
                },
                {
                    "run_id": "run-1", "sequence": 2,
                    "frame": {"at": "2026-09-29T07:00:00+00:00",
                              "context": {"breadth_5d": 0.61, "selection_day": False}},
                    "signal": {"scores": {"000001": 0.0}},
                    "planned_orders": [],
                    "execution": {"fills": [{
                        "order_id": "o1", "at": "2026-09-29T07:00:00+00:00",
                        "instrument": "000001", "side": "buy", "quantity": "1200",
                        "price": "10.01", "fee": "5",
                        "fee_components": {"commission": "5", "stamp_duty": "0", "transfer_fee": "0"},
                    }], "rejections": [], "remaining": []},
                    "account": {"cash": "87983", "equity": "99983",
                                "positions": {"000001": "1200"}},
                },
            ]
            payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
            events.write_text(payload, encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": 1,
                "run_id": "run-1",
                "artifact_kind": "ENGINEERING_GOLDEN",
                "events_file": "events.jsonl",
                "events_sha256": sha256(payload.encode()).hexdigest(),
                "initial_cash": "100000",
                "strategy": {
                    "id": "breadth_low_amount_reversal",
                    "name": "低成交额20日反转",
                    "parameters": {"positions": 8},
                    "rules": ["市场广度达到60%才允许买入", "市场广度低于45%全部卖出"],
                },
                "validation": {
                    "engineering": "PASSED",
                    "historical": "BLOCKED",
                    "historical_reason": "复权数据门禁尚未通过",
                },
            }, ensure_ascii=False), encoding="utf-8")

            report = load_closed_loop_report(manifest)

            self.assertEqual(report["run"]["run_id"], "run-1")
            self.assertEqual(report["run"]["steps"], 2)
            self.assertEqual(report["run"]["events_sha256"], sha256(payload.encode()).hexdigest())
            self.assertEqual(report["strategy"]["parameters"], {"positions": 8})
            self.assertEqual(report["validation"]["historical"], "BLOCKED")
            self.assertEqual(report["timeline"][0]["action"], "生成1笔买入订单")
            self.assertEqual(report["timeline"][1]["action"], "1笔买入成交")
            self.assertEqual(report["timeline"][1]["cash"], "87983")
            self.assertEqual(report["timeline"][1]["position_count"], 1)
            self.assertEqual(report["trades"][0]["commission"], "5")
            self.assertEqual(report["trades"][0]["sequence"], 2)

    def test_hash_mismatch_stops_instead_of_showing_unverified_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": 1, "run_id": "run-1", "artifact_kind": "ENGINEERING_GOLDEN",
                "events_file": "events.jsonl", "events_sha256": "0" * 64,
                "initial_cash": "100000", "strategy": {"id": "s", "name": "s",
                "parameters": {}, "rules": []}, "validation": {"engineering": "PASSED",
                "historical": "BLOCKED", "historical_reason": "blocked"},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_closed_loop_report(manifest)


if __name__ == "__main__":
    unittest.main()
