import threading
import tempfile
import time
import json
import unittest
from unittest import mock

from pathlib import Path
from protocal import build_data
import collect.fetch as fetch_manager
from collect.fetch import FetchManager


class FetchManagerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = build_data(Path(directory.name))
        self.path = self.data.path('state', 'schedules', 'lake.json')
        self.path.parent.mkdir(parents=True)

    def test_close_stops_the_scheduler_without_rewriting_configuration(self):
        path = self.path
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": "2000-01-01T00:00:00"}, fp)
        manager = FetchManager(self.data)
        called = threading.Event()
        manager.start_scheduler(lambda: called.set() or False)
        self.assertTrue(called.wait(1))

        manager.close()

        self.assertTrue(manager.schedule()["enabled"])
        self.assertTrue(manager._scheduler_stop.is_set())

    def test_begin_is_atomic_before_worker_runs(self):
        manager = FetchManager(self.data)
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
        manager = FetchManager(self.data)
        first = manager.begin(lambda: True)
        self.assertTrue(first[0])
        stopped, _ = manager.stop()
        self.assertTrue(stopped)
        manager.finish(lambda: None)
        self.assertEqual(manager.snapshot()["status"], "stopped")

    def test_schedule_is_persisted_and_invalid_interval_rejected(self):
        path = self.path
        manager = FetchManager(self.data)
        with self.assertRaises(ValueError):
            manager.set_schedule(True, 59)
        value = manager.set_schedule(True, 60)
        self.assertEqual(value["enabled"], True)
        self.assertEqual(value["interval_seconds"], 60)
        loaded = FetchManager(self.data)
        self.assertEqual(loaded.schedule()["enabled"], True)
        self.assertEqual(loaded.schedule()["interval_seconds"], 60)

    def test_corrupt_schedule_is_not_silently_disabled(self):
        path = self.path
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("not-json")
        with self.assertRaises(ValueError):
            FetchManager(self.data)

    def test_restart_uses_saved_due_time(self):
        path = self.path
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": "2000-01-01T00:00:00"}, fp)
        manager = FetchManager(self.data)
        called = threading.Event()
        manager.start_scheduler(lambda: called.set() or True)
        self.assertTrue(called.wait(1))
        manager.set_schedule(False, 60)

    def test_callback_failure_is_visible_and_disables_scheduler(self):
        path = self.path
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"enabled": True, "interval_seconds": 60,
                       "last_run_at": None, "next_run_at": "2000-01-01T00:00:00"}, fp)
        manager = FetchManager(self.data)
        manager.start_scheduler(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        deadline = time.time() + 1
        while time.time() < deadline and manager.schedule()["enabled"]:
            time.sleep(0.01)
        self.assertFalse(manager.schedule()["enabled"])
        self.assertIn("boom", manager.schedule()["schedule_error"])

    def test_rejected_run_reports_error_and_waits_for_next_interval(self):
        due = "2000-01-01T00:00:00"
        self.data.write('state', 'schedules', 'lake.json', json.dumps({
            'enabled': True, 'interval_seconds': 60, 'last_run_at': None, 'next_run_at': due}).encode())
        manager = FetchManager(self.data)
        written = threading.Event()
        original = manager._persist_schedule
        def persist():
            original()
            written.set()
        attempt = mock.Mock(return_value=False)
        with mock.patch.object(manager, '_persist_schedule', side_effect=persist):
            manager.start_scheduler(attempt)
            try:
                self.assertTrue(written.wait(1))
                self.assertNotEqual(manager.schedule()['next_run_at'], due)
                self.assertIsNone(manager.schedule()['last_run_at'])
                self.assertEqual(manager.schedule()['schedule_error'], 'collection not accepted')
                attempt.assert_called_once()
            finally:
                manager.close()

    def test_rapid_schedule_changes_do_not_raise_scheduler_thread_errors(self):
        path = self.path
        manager = FetchManager(self.data)
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
