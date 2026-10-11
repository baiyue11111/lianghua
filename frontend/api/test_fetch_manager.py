import threading
import tempfile
import time
import json
import unittest
from unittest import mock

import fetch_manager
from fetch_manager import FetchManager


class FetchManagerTests(unittest.TestCase):
    def test_begin_is_atomic_before_worker_runs(self):
        manager = FetchManager(tempfile.mktemp())
        reserved = {"value": False}

        def reserve():
            if reserved["value"]:
                return False
            reserved["value"] = True
            return True

        first = manager.begin(reserve)
        second = manager.begin(reserve)
        self.assertTrue(first[0])
        self.assertFalse(second[0])
        self.assertTrue(manager.snapshot()["running"])

    def test_stop_is_visible_and_finish_keeps_stopped_status(self):
        manager = FetchManager(tempfile.mktemp())
        first = manager.begin(lambda: True)
        self.assertTrue(first[0])
        stopped, _ = manager.stop()
        self.assertTrue(stopped)
        manager.finish(lambda: None)
        self.assertEqual(manager.snapshot()["status"], "stopped")

    def test_schedule_is_persisted_and_invalid_interval_rejected(self):
        path = tempfile.mktemp()
        manager = FetchManager(path)
        with self.assertRaises(ValueError):
            manager.set_schedule(True, 59)
        value = manager.set_schedule(True, 60)
        self.assertEqual(value["enabled"], True)
        self.assertEqual(value["interval_seconds"], 60)
        loaded = FetchManager(path)
        self.assertEqual(loaded.schedule()["enabled"], True)
        self.assertEqual(loaded.schedule()["interval_seconds"], 60)

    def test_corrupt_schedule_is_not_silently_disabled(self):
        path = tempfile.mktemp()
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("not-json")
        with self.assertRaises(ValueError):
            FetchManager(path)

    def test_restart_uses_saved_due_time(self):
        path = tempfile.mktemp()
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": "2000-01-01T00:00:00"}, fp)
        manager = FetchManager(path)
        called = threading.Event()
        manager.start_scheduler(lambda: called.set() or True)
        self.assertTrue(called.wait(1))
        manager.set_schedule(False, 60)

    def test_callback_failure_is_visible_and_disables_scheduler(self):
        path = tempfile.mktemp()
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": "2000-01-01T00:00:00"}, fp)
        manager = FetchManager(path)
        manager.start_scheduler(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        deadline = time.time() + 1
        while time.time() < deadline and manager.schedule()["enabled"]:
            time.sleep(0.01)
        self.assertFalse(manager.schedule()["enabled"])
        self.assertIn("boom", manager.schedule()["schedule_error"])

    def test_rejected_run_stays_due_and_retries(self):
        path = tempfile.mktemp()
        due = "2000-01-01T00:00:00"
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": due}, fp)
        manager = FetchManager(path)
        first = threading.Event()
        second = threading.Event()
        calls = 0

        def attempt():
            nonlocal calls
            calls += 1
            if calls == 1:
                first.set()
                return False
            second.set()
            return True

        with mock.patch.object(fetch_manager, "REJECTED_RETRY_SECONDS", 0.01):
            manager.start_scheduler(attempt)
            try:
                self.assertTrue(first.wait(1))
                self.assertEqual(manager.schedule()["next_run_at"], due)
                self.assertIsNone(manager.schedule()["last_run_at"])
                self.assertTrue(second.wait(1), "rejected due run was not retried")
                deadline = time.time() + 1
                while manager.schedule()["next_run_at"] == due and time.time() < deadline:
                    time.sleep(0.01)
                self.assertNotEqual(manager.schedule()["next_run_at"], due)
            finally:
                manager.set_schedule(False, 60)

    def test_rapid_schedule_changes_do_not_raise_scheduler_thread_errors(self):
        path = tempfile.mktemp()
        manager = FetchManager(path)
        errors = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_value)
        try:
            manager.start_scheduler(lambda: True)
            for _ in range(5):
                manager.set_schedule(True, 60)
                manager.set_schedule(False, 60)
            time.sleep(0.05)
        finally:
            threading.excepthook = previous_hook
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
