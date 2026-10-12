"""Protect actual completed counts, immutable provenance and exact replay."""
from pathlib import Path
import tempfile
import unittest

from research_registry import ResearchRegistry
from dynamic_selection import StateEvent
from datetime import date


class ResearchRegistryTests(unittest.TestCase):
    def test_unvalidated_dynamic_selector_cannot_enter_enabled_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchRegistry(Path(directory))
            run = dict(strategy_id="s", family_id="f", config_hash="a"*64,
                       input_hash="b"*64, code_hash="c"*64, completed_on="2026-09-25",
                       status="completed", data_verified=True, accounting_verified=True,
                       historical_qualified=True, validation_artifact_hash="d"*64, metrics={})
            run_id = store.record_run(run)
            events = (StateEvent(date(2026, 9, 26), "active", "activated", "entry_conditions_confirmed", 2, .1, -.02, 0),)
            store.record_states(run_id, "e"*64, "f"*64, events, gate_verified=False)
            self.assertEqual(store.dashboard("2026-09-26")["active"], 0)

    def test_future_states_are_excluded_and_verified_pause_is_counted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchRegistry(Path(directory))
            run = dict(strategy_id="s", family_id="f", config_hash="a"*64,
                       input_hash="b"*64, code_hash="c"*64, completed_on="2026-09-25",
                       status="completed", data_verified=True, accounting_verified=True,
                       historical_qualified=True, validation_artifact_hash="d"*64, metrics={})
            run_id = store.record_run(run)
            events = (StateEvent(date(2026, 9, 25), "active", "activated", "entry_conditions_confirmed", 2, .1, -.02, 0),
                      StateEvent(date(2026, 9, 26), "paused", "paused", "drawdown_limit", 0, -.1, -.2, 0),
                      StateEvent(date(2026, 9, 27), "active", "resumed", "recent_performance_recovered", 2, .1, -.02, 0))
            first = store.record_states(run_id, "e"*64, "f"*64, events, gate_verified=True)
            self.assertEqual(first, store.record_states(run_id, "e"*64, "f"*64, events, gate_verified=True))
            report = store.dashboard("2026-09-26")
            self.assertEqual(report["today_paused"], 1)
            self.assertEqual(report["active"], 0)
            self.assertEqual(report["today_resumed"], 0)
            stale = store.dashboard("2026-09-28")
            self.assertEqual(stale["active"], 0)
            self.assertFalse(stale["strategies"][0]["assessment_current"])
    def test_replay_does_not_inflate_daily_strategy_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchRegistry(Path(directory))
            run = dict(strategy_id="s", family_id="f", config_hash="a"*64,
                       input_hash="b"*64, code_hash="c"*64, completed_on="2026-09-26",
                       status="completed", data_verified=True, accounting_verified=True,
                       historical_qualified=False, metrics={"sharpe": .5})
            first = store.record_run(run)
            self.assertEqual(first, store.record_run(run))
            report = store.dashboard("2026-09-26")
            self.assertEqual(report["today_completed"], 1)
            self.assertEqual(report["today_families"], 1)
            self.assertEqual(report["active"], 0)

    def test_raw_invalid_result_is_not_counted_as_completed_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchRegistry(Path(directory))
            run = dict(strategy_id="s", family_id="f", config_hash="a"*64,
                       input_hash="b"*64, code_hash="c"*64, completed_on="2026-09-26",
                       status="completed", data_verified=False, accounting_verified=False,
                       historical_qualified=False, metrics={})
            store.record_run(run)
            self.assertEqual(store.dashboard("2026-09-26")["today_completed"], 0)
            self.assertEqual(store.dashboard("2026-09-26")["today_invalid"], 1)

    def test_corrupt_saved_record_fails_instead_of_showing_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchRegistry(Path(directory))
            (Path(directory)/"runs").mkdir()
            (Path(directory)/"runs"/"bad.json").write_text("{}")
            with self.assertRaises(ValueError):
                store.dashboard("2026-09-26")
