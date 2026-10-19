"""Public source snapshots are evidence, never an automatic cn_a replacement."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from market import build_data

from collect.methods import canonical, digest, capture, make_request, parse_bars, snapshot, validate_snapshot


class EastmoneyEvidenceTests(unittest.TestCase):
    def test_capture_processes_every_market_in_the_same_master_without_market_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = build_data(root)
            master = data.path("sources", "cn_a", "master.parquet")
            output = data.path("sources", "bar_evidence", "manifest.json").parent
            master_ref = data.write("sources", "cn_a", "master.parquet", pd.DataFrame({"code": ["600000", "000001", "920000"],
                          "exchange": ["SH", "SZ", "BJ"]}).to_parquet(index=False))
            for code, exchange in [("600000", "SH"), ("000001", "SZ"), ("920000", "BJ")]:
                request = make_request(code, exchange, "20260901", "20260918", "raw")
                raw = json.dumps({"rc": 0, "data": {"code": code, "klines": [
                    "2026-09-01,10,10.2,10.3,9.9,100,101000,4,2,0.2,1"]}})
                data.write("sources", "bar_evidence", digest(request) + ".json", canonical(snapshot(request, raw)) + b"\n")
            result = capture(data, [("600000", "SH"), ("000001", "SZ"), ("920000", "BJ")],
                             "20260901", "20260918", "raw", master_sha256=master_ref.sha256,
                             fetcher=lambda _: self.fail("saved evidence must not be fetched again"))
            self.assertEqual(result["failed_or_empty"], 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual([row["code"] for row in manifest["responses"]],
                             ["000001", "600000", "920000"])
            self.assertEqual([row["status"] for row in manifest["responses"]],
                             ["STRUCTURALLY_VALID"] * 3)
            self.assertEqual(manifest["master_sha256"], hashlib.sha256(master.read_bytes()).hexdigest())
            self.assertEqual(manifest["release_status"], "NOT_RELEASED")

    def setUp(self):
        self.request = make_request("920000", "BJ", "20260901", "20260918", "raw")
        self.raw = '{"rc":0,"data":{"code":"920000","name":"安徽凤凰","klines":["2026-09-01,13.89,14.19,14.19,13.75,8672,12190931.81,3.20,3.28,0.45,1.51"]}}'

    def test_preserves_raw_snapshot_and_converts_hands_to_shares(self):
        evidence = snapshot(self.request, self.raw)
        validate_snapshot(evidence, self.request)
        self.assertEqual(evidence["raw_response"], self.raw)
        bars = parse_bars(evidence)
        self.assertEqual(bars[0]["volume"], 867200)
        self.assertEqual(bars[0]["amount"], 12190931.81)
        self.assertEqual(bars[0]["close"], 14.19)
        self.assertEqual(bars[0]["price_basis"], "raw")
        adjusted = make_request("920000", "BJ", "20260901", "20260918", "hfq")
        self.assertNotEqual(adjusted, self.request)
        self.assertEqual(parse_bars(snapshot(adjusted, self.raw))[0]["price_basis"], "hfq")

    def test_rejects_tampered_response_and_wrong_security(self):
        evidence = snapshot(self.request, self.raw)
        broken = copy.deepcopy(evidence)
        broken["raw_response"] = broken["raw_response"].replace("14.19", "99.99")
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_snapshot(broken, self.request)
        wrong = snapshot(self.request, self.raw.replace('"920000"', '"920001"'))
        with self.assertRaisesRegex(ValueError, "code"):
            parse_bars(wrong)

    def test_refuses_provider_error_duplicate_dates_and_impossible_prices(self):
        with self.assertRaises(ValueError):
            parse_bars(snapshot(self.request, '{"rc":1,"data":null}'))
        self.assertEqual(parse_bars(snapshot(self.request, '{"rc":0,"data":null}')), [])
        import json
        data = json.loads(self.raw)
        data["data"]["klines"] *= 2
        with self.assertRaisesRegex(ValueError, "date"):
            parse_bars(snapshot(self.request, json.dumps(data)))
        with self.assertRaisesRegex(ValueError, "OHLC"):
            parse_bars(snapshot(self.request, self.raw.replace("13.89,14.19", "20.00,14.19")))


if __name__ == "__main__":
    unittest.main()
