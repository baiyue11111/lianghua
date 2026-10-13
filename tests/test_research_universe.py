import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tools.research_universe import build_universe


class ResearchUniverseTests(unittest.TestCase):
    def test_build_preserves_master_and_separates_supported_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            output = Path(tmp) / "universe"
            (lake / "daily").mkdir(parents=True)
            pd.DataFrame([
                {"code": "000001", "name": "平安", "exchange": "SZ"},
                {"code": "600001", "name": "上证", "exchange": "SH"},
                {"code": "302132", "name": "深新股", "exchange": "SZ"},
                {"code": "920001", "name": "北交", "exchange": "BJ"},
            ]).to_parquet(lake / "stock_info.parquet", index=False)
            pd.DataFrame([
                {"code": "000001", "date": "2025-01-02"},
                {"code": "000001", "date": "2025-01-02"},
                {"code": "302132", "date": "2025-01-02"},
            ]).to_parquet(lake / "daily" / "2025.parquet", index=False)

            manifest = build_universe(lake, output)
            saved = json.loads((output / "universe.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest, saved)
            self.assertEqual(manifest["master"]["count"], 4)
            self.assertEqual(manifest["daily"]["unique_code_count"], 2)
            self.assertEqual(manifest["supported"]["codes"], ["000001", "302132", "600001"])
            self.assertEqual(manifest["unsupported"]["codes"], ["920001"])
            self.assertEqual(manifest["missing_daily"], [{"code": "600001", "name": "上证", "exchange": "SH"}, {"code": "920001", "name": "北交", "exchange": "BJ"}])
            self.assertEqual(manifest["exchange_coverage"]["SZ"]["daily_count"], 2)
            self.assertEqual(manifest["duplicate_daily_keys"], [{"code": "000001", "date": "2025-01-02", "count": 2}])
            self.assertEqual((output / "supported_codes.txt").read_text(encoding="utf-8"), "000001\n302132\n600001\n")
            self.assertEqual((output / "unsupported_codes.txt").read_text(encoding="utf-8"), "920001\n")

    def test_duplicate_master_keys_are_reported_without_dropping_mother_set_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            output = Path(tmp) / "universe"
            (lake / "daily").mkdir(parents=True)
            pd.DataFrame([
                {"code": "000001", "name": "旧名", "exchange": "SZ"},
                {"code": "000001", "name": "新名", "exchange": "SZ"},
            ]).to_parquet(lake / "stock_info.parquet", index=False)
            pd.DataFrame([{ "code": "000001", "date": "2025-01-02" }]).to_parquet(lake / "daily" / "2025.parquet", index=False)

            manifest = build_universe(lake, output)

            self.assertFalse(manifest["valid"])
            self.assertEqual(manifest["master"]["count"], 2)
            self.assertEqual(manifest["master"]["unique_count"], 1)
            self.assertEqual(manifest["duplicate_master_keys"], [{"code": "000001", "count": 2}])
            self.assertFalse((output / "supported_codes.txt").exists())
            self.assertFalse((output / "unsupported_codes.txt").exists())


if __name__ == "__main__":
    unittest.main()
