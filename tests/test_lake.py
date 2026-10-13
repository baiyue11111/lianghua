import importlib.util
import io
import os
import tempfile
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lake_under_test", ROOT / "src" / "collect" / "lake.py")
lake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lake
spec.loader.exec_module(lake)


class LakeTests(TestCase):
    def setUp(self):
        from protocal import build_data
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lake.bind(build_data(Path(directory.name)))

    def test_automatic_plan_refreshes_each_run(self):
        args = SimpleNamespace(factors=False, start=None, codes=None, force=False)
        first = [lake.Task("600000", "auto", "2026-09-11", "2026-09-18")]
        second = [lake.Task("600000", "auto", "2026-09-18", "2026-09-21")]
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(lake, "TASKS_JSON", str(Path(directory) / "tasks.json")), \
                    mock.patch.object(lake, "build_auto_tasks", side_effect=[first, second]):
                self.assertEqual(lake.build_tasks(args), first)
                self.assertEqual(lake.build_tasks(args), second)

    def test_distinct_date_windows_have_distinct_cache_identity(self):
        backfill = lake.Task("600000", "auto", "2013-01-01", "2014-12-31")
        incremental = lake.Task("600000", "auto", "2026-09-11", "2026-09-18")
        self.assertNotEqual(backfill.cache_file, incremental.cache_file)

    def test_today_is_resolved_at_call_time(self):
        with mock.patch.object(lake.time, "strftime", side_effect=["2026-09-19", "2026-09-20"]):
            self.assertEqual(lake.today(), "2026-09-19")
            self.assertEqual(lake.today(), "2026-09-20")

    def test_failed_query_is_not_retried(self):
        task = lake.Task("600036", "fix", "2026-01-01", "2026-01-02")
        with mock.patch.object(lake, "daily_frame", side_effect=RuntimeError("down")) as query:
            with self.assertRaisesRegex(RuntimeError, "down"):
                lake.bs_query(task.code, task)
            query.assert_called_once()

    def test_adjust_cache_from_previous_day_is_stale(self):
        with mock.patch.object(lake, "daily_mtime", return_value=100.0), \
                mock.patch.object(lake, "_today", return_value="2026-09-20"), \
                mock.patch.object(lake.os.path, "getmtime", return_value=time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, -1))):
            self.assertFalse(lake._adjust_cache_is_fresh("/tmp/factor.parquet"))

    def test_merge_rejects_fill_for_missing_year(self):
        with self.assertRaisesRegex(ValueError, "missing target partition"):
            lake._validate_merge_inputs(
                {"2026": {"fill": True, "exists": False}}
            )

    def _merge_fixture(self, *, duplicate_untouched=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from protocal import build_data
        lake.bind(build_data(Path(tmp.name)))
        root = Path(lake.LAKE)
        daily = root / "daily"
        cache = root / "_incr_cache"
        daily.mkdir(parents=True)
        cache.mkdir()
        si = pd.DataFrame({
            "code": ["000001"], "name": ["平安银行"],
            "exchange": ["SZ"], "status": ["listed"],
        })
        si.to_parquet(root / "stock_info.parquet", index=False)
        original = pd.DataFrame({
            "code": ["000001", "000001"] if duplicate_untouched else ["000001"],
            "date": ["2025-01-02", "2025-01-02"] if duplicate_untouched else ["2025-01-02"],
            "open": [10.0] * (2 if duplicate_untouched else 1),
            "high": [10.0] * (2 if duplicate_untouched else 1),
            "low": [10.0] * (2 if duplicate_untouched else 1),
            "close": [10.0] * (2 if duplicate_untouched else 1),
            "volume": [1.0] * (2 if duplicate_untouched else 1),
            "amount": [100.0] * (2 if duplicate_untouched else 1),
            "pctChg": [0.0] * (2 if duplicate_untouched else 1),
            "pool": ["current"] * (2 if duplicate_untouched else 1),
        })
        original_year = "2024" if duplicate_untouched else "2025"
        original.to_parquet(daily / f"{original_year}.parquet", index=False)
        increment = pd.DataFrame({
            "date": ["2025-01-03"], "open": [11.0], "high": [11.0],
            "low": [11.0], "close": [11.0], "volume": [1.0],
            "amount": [110.0], "pctChg": [1.0],
        })
        increment.to_parquet(cache / "000001_fix.parquet", index=False)
        patches = mock.patch.multiple(
            lake, LAKE=str(root), DAILY=str(daily), DAILY_GLOB=str(daily / "*.parquet"),
            CACHE=str(cache), STOCK_INFO=str(root / "stock_info.parquet"),
            VALUATION=str(root / "valuation.parquet"),
            FUNDAMENTALS=str(root / "fundamentals.parquet"),
            LATEST=str(root / "latest.parquet"), README=str(root / "README.md"),
            PROFILE_JSON=str(root / "profile.json"),
            PROBED_JSON=str(root / "probed.json"),
            TASKS_JSON=str(cache / "tasks_v2.json"),
            NEW_STOCKS_JSON=str(cache / "new_stocks.json"),
        )
        patches.start()
        self.addCleanup(patches.stop)
        return tmp, root, daily, cache

    def test_merge_dry_run_validates_candidate_and_reports_candidate_totals(self):
        _, root, daily, cache = self._merge_fixture()
        del root
        before = pd.read_parquet(daily / "2025.parquet")
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(lake.cmd_merge(SimpleNamespace(dry_run=True)), 0)
        report = output.getvalue()
        self.assertIn("合并后行数: 2", report)
        self.assertIn("日期范围: 2025-01-02 ~ 2025-01-03", report)
        pd.testing.assert_frame_equal(pd.read_parquet(daily / "2025.parquet"), before)
        self.assertTrue((cache / "000001_fix.parquet").exists())

    def test_merge_validates_all_candidates_before_replacing_any_partition(self):
        _, root, daily, cache = self._merge_fixture(duplicate_untouched=True)
        del root, cache
        before = pd.read_parquet(daily / "2024.parquet")
        with self.assertRaisesRegex(AssertionError, "重复"):
            lake.cmd_merge(SimpleNamespace(dry_run=False))
        pd.testing.assert_frame_equal(pd.read_parquet(daily / "2024.parquet"), before)

    def test_failed_merge_cleans_candidates_before_returning_error(self):
        _, root, _, _ = self._merge_fixture(duplicate_untouched=True)
        try:
            lake.cmd_merge(SimpleNamespace(dry_run=False))
        except AssertionError:
            self.assertEqual(list(root.glob(".merge-*")), [])
        else:
            self.fail("duplicate candidate must be rejected")

    def test_merge_keeps_cache_file_created_during_run(self):
        _, root, daily, cache = self._merge_fixture()
        del root, daily
        new_cache = cache / "000002_fix.parquet"
        original_replace = os.replace

        def replace_and_create(src, dst):
            original_replace(src, dst)
            if str(dst).endswith("2025.parquet") and not new_cache.exists():
                pd.DataFrame({"date": ["2025-01-04"], "close": [12.0]}).to_parquet(
                    new_cache, index=False
                )

        with mock.patch.object(lake.os, "replace", side_effect=replace_and_create):
            self.assertEqual(lake.cmd_merge(SimpleNamespace(dry_run=False)), 0)
        self.assertTrue(new_cache.exists())
