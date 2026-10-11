import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frontend import web as server


class ServerApiTests(unittest.TestCase):
    def test_auto_worker_preserves_planned_backfill_end_date(self):
        task = server.lake.Task("600000", "auto", "2013-01-01", "2014-12-31")
        seen = []
        provider = SimpleNamespace(login=lambda: None, logout=lambda: None)
        with patch.dict(sys.modules, {"baostock": provider}), \
             patch.object(server.lake, "build_tasks", return_value=[task]), \
             patch.object(server.lake, "pending_tasks", side_effect=lambda tasks, **kw: tasks), \
             patch.object(server.lake, "bs_query_retry", side_effect=lambda code, task: seen.append(task)):
            server._fetch_worker(server.FetchRequest(mode="auto"), threading.Event())
        self.assertEqual(seen[0].end, "2014-12-31")

    def test_continuous_reports_latest_date_with_market_coverage(self):
        coverage = {"latest_data_date": "2026-09-18", "latest_data_stock_count": 1,
                    "tracked_stock_count": 5472, "median_stock_date": "2026-09-11"}
        with patch.object(server.lake, "latest_data_coverage", return_value=coverage):
            result = server.fetch_continuous()
        self.assertEqual(result["latest_data_date"], "2026-09-18")
        self.assertEqual(result["latest_data_stock_count"], 1)
        self.assertEqual(result["tracked_stock_count"], 5472)
        self.assertEqual(result["median_stock_date"], "2026-09-11")

        empty = {"latest_data_date": None, "latest_data_stock_count": 0,
                 "tracked_stock_count": 0, "median_stock_date": None}
        with patch.object(server.lake, "latest_data_coverage", return_value=empty):
            result = server.fetch_continuous()
        self.assertIsNone(result["latest_data_date"])

    def test_continuous_exposes_latest_data_read_error(self):
        with patch.object(server.lake, "latest_data_coverage", side_effect=RuntimeError("lake unavailable")):
            result = server.fetch_continuous()
        self.assertIsNone(result["latest_data_date"])
        self.assertIn("lake unavailable", result["latest_data_error"])

    def test_scheduled_start_rejection_is_visible(self):
        before = server.FETCH_MANAGER.snapshot()
        with patch.object(server, "start_fetch", return_value={"ok": False, "message": "已有任务进行中"}):
            self.assertFalse(server._scheduled_fetch())
        self.assertEqual(server.FETCH_MANAGER.snapshot()["status"], before["status"])
        self.assertIn("调度未启动", server._SCHEDULE_ATTEMPT_ERROR)

    def test_scheduled_start_acceptance_is_reported_to_scheduler(self):
        with patch.object(server, "start_fetch", return_value={"ok": True}):
            self.assertTrue(server._scheduled_fetch())
        self.assertIsNone(server._SCHEDULE_ATTEMPT_ERROR)

    def test_failed_fetch_does_not_auto_merge(self):
        req = server.FetchRequest(mode="auto")
        class ImmediateThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        with patch.object(server, "threading") as thread_module, \
             patch.object(server, "_fetch_worker", side_effect=lambda req, stop: server.FETCH_MANAGER.update(failed=["600000"])), \
             patch.object(server, "start_merge") as merge:
            thread_module.Thread = ImmediateThread
            self.assertTrue(server.start_fetch(req)["ok"])
        merge.assert_not_called()
        self.assertEqual(server.FETCH_MANAGER.snapshot()["status"], "failed")

    def test_stopped_fetch_does_not_auto_merge(self):
        req = server.FetchRequest(mode="auto")
        class ImmediateThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        with patch.object(server, "threading") as thread_module, \
             patch.object(server, "_fetch_worker", side_effect=lambda req, stop: stop.set()), \
             patch.object(server, "start_merge") as merge:
            thread_module.Thread = ImmediateThread
            self.assertTrue(server.start_fetch(req)["ok"])
        merge.assert_not_called()
        self.assertEqual(server.FETCH_MANAGER.snapshot()["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
