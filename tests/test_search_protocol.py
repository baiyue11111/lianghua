from __future__ import annotations

import asyncio
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ui = import_module("frontend.ui-protocol")


def await_protocol(protocol, operation, data):
    message = asyncio.run(protocol.send(ui.UiRequest(
        f"{operation}-request", operation,
        f"urn:lianghua:ui:input:{operation}:v1", data,
    )))[0]
    if message.kind != "resource" and message.kind != "collection":
        raise AssertionError(message.data)
    return message.data


class SearchProtocolTests(unittest.TestCase):
    def _fixture_protocol(self, root):
        from market import DataBus
        from backend import KernelBacktestPort, Modules, Protocol
        from storage import DataStore
        from tests.history_fixture import make_snapshot
        from tests.fee_fixture import configured_settlement

        root = Path(root)
        config = make_snapshot(root)
        bus = DataBus(DataStore(root))
        backtests = KernelBacktestPort(bus, configured_settlement())
        empty = type("Empty", (), {"handle": lambda self, operation, data: {}})()
        return bus, Protocol(Modules(backtests, backtests, empty, empty, empty))

    def _search_payload(self, run_id, candidates):
        return {
            "run_id": run_id,
            "module": "plugins.strategies.moving_average_cross",
            "params": {"fast_window": 1, "slow_window": 3},
            "dataset_id": "fixture", "start": "2020-01-31", "end": "2020-02-03",
            "initial_cash": "100000",
            "search": {
                "updater": {
                    "module": "core.calculation:Calculation", "method": "propose",
                    "params": {"candidates": candidates},
                },
                "max_trials": len(candidates), "metric": "total_return", "direction": "max",
            },
        }

    def test_empty_candidate_is_rejected_and_does_not_create_a_trial(self):
        with TemporaryDirectory() as root:
            bus, protocol = self._fixture_protocol(root)
            payload = self._search_payload("empty-candidate", [{}])
            message = asyncio.run(protocol.send(ui.UiRequest(
                "empty-candidate-request", "backtest.run",
                "urn:lianghua:ui:input:backtest.run:v1", payload,
            )))[0]

            self.assertEqual(message.kind, "problem")
            self.assertEqual(message.data["code"], "search_failed")
            self.assertFalse(bus.path("runs", "empty-candidate-trial-0001", "manifest.json").exists())
            failed = json.loads(bus.read("runs", "empty-candidate", "failed.json"))
            self.assertEqual(failed["partial"]["failed_values"], {})
            self.assertEqual(failed["partial"]["trial_index"], 1)

    def test_failed_candidate_evidence_keeps_candidate_and_trial_index(self):
        with TemporaryDirectory() as root:
            bus, protocol = self._fixture_protocol(root)
            payload = self._search_payload("bad-shape", [
                {"fast_window": 1, "slow_window": 3},
                {"fast_window": [], "slow_window": 3},
            ])
            message = asyncio.run(protocol.send(ui.UiRequest(
                "bad-shape-request", "backtest.run",
                "urn:lianghua:ui:input:backtest.run:v1", payload,
            )))[0]

            self.assertEqual(message.kind, "problem")
            self.assertEqual(message.data["code"], "search_failed")
            failed = json.loads(bus.read("runs", "bad-shape", "failed.json"))
            self.assertEqual(
                failed["partial"]["completed_trials"][0]["run_id"],
                "bad-shape-trial-0001",
            )
            self.assertEqual(
                failed["partial"]["failed_values"],
                {"fast_window": [], "slow_window": 3},
            )
            self.assertEqual(failed["partial"]["trial_index"], 2)
            self.assertTrue(bus.path("runs", "bad-shape-trial-0001", "manifest.json").exists())
            self.assertFalse(bus.path("runs", "bad-shape", "manifest.json").exists())

    def test_request_accepts_only_the_declared_search_shape(self):
        from protocol import parse_backtest_request

        value = parse_backtest_request({
            "run_id": "search",
            "module": "plugins.strategies.moving_average_cross",
            "params": {"fast_window": 1, "slow_window": 2},
            "dataset_id": "fixture", "start": "2020-01-31", "end": "2020-02-03",
            "initial_cash": "100000",
            "search": {
                "updater": {
                    "module": "core.calculation:Calculation", "method": "propose",
                    "params": {"candidates": [{"fast_window": 1}, {"fast_window": 2}]},
                },
                "max_trials": 2, "metric": "total_return", "direction": "max",
            },
        })

        from protocol import BacktestRequest

        self.assertIsInstance(value, BacktestRequest)

    def test_protocol_search_saves_each_trial_and_base_summary(self):
        from market import DataBus
        from backend import KernelBacktestPort, Modules, Protocol, _DIRECTORY
        from storage import DataStore
        from tests.history_fixture import make_snapshot
        from tests.fee_fixture import configured_settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            backtests = KernelBacktestPort(bus, configured_settlement())
            empty = type("Empty", (), {"handle": lambda self, operation, data: {}})()
            protocol = Protocol(Modules(backtests, backtests, empty, empty, empty))
            request = ui.UiRequest(
                "search-request", "backtest.run",
                "urn:lianghua:ui:input:backtest.run:v1",
                {
                    "run_id": "search",
                    "module": "plugins.strategies.moving_average_cross",
                    "params": {"fast_window": 1, "slow_window": 3},
                    "dataset_id": "fixture", "start": "2020-01-31", "end": "2020-02-03",
                    "initial_cash": "100000",
                    "search": {
                        "updater": {
                            "module": "core.calculation:Calculation", "method": "propose",
                            "params": {"candidates": [
                                {"fast_window": 1, "slow_window": 3},
                                {"fast_window": 2, "slow_window": 3},
                            ]},
                        },
                        "max_trials": 2, "metric": "total_return", "direction": "max",
                    },
                },
            )

            messages = asyncio.run(protocol.send(request))
            self.assertEqual(messages[0].kind, "resource")
            value = messages[0].data
            self.assertEqual(value["run_id"], "search")
            self.assertEqual(len(value["search"]["trials"]), 2)
            self.assertEqual(value["search"]["best_run_id"], value["search"]["trials"][value["search"]["best_index"]]["run_id"])
            self.assertNotIn("validation", value["search"])
            self.assertNotIn("out_of_sample", value["search"])
            identities = {ref.identity for ref in bus.list("runs")}
            self.assertTrue({"search", "search-trial-0001", "search-trial-0002"} <= identities)
            manifest = json.loads(bus.read("runs", "search", "manifest.json"))
            self.assertEqual(manifest["parameters"], value["parameters"])
            runs = await_protocol(protocol, "research.runs", {})
            self.assertIn("search", {item["id"] for item in runs["items"]})
            detail = await_protocol(protocol, "research.run", {"id": "search"})
            self.assertEqual(detail["parameters"], value["parameters"])
            json.dumps(value, allow_nan=False)

    def test_second_local_staticmethod_uses_score_through_protocol_with_summary_and_hash(self):
        from plugins import data as plugin_data

        class ProbeUpdater:
            @staticmethod
            def pick(trials, *, candidates):
                if not trials:
                    return candidates[0]
                return candidates[1] if trials[-1].score < 0 else None

        with TemporaryDirectory() as root:
            bus, protocol = self._fixture_protocol(root)
            candidates = [
                {"fast_window": 1, "slow_window": 3, "exposure": 1},
                {"fast_window": 1, "slow_window": 3, "exposure": 0},
            ]
            payload = self._search_payload("plugin-search", candidates)
            payload["search"]["updater"] = {
                "module": "plugins.data:ProbeUpdater",
                "method": "pick",
                "params": {"candidates": candidates},
            }
            with patch.object(plugin_data, "ProbeUpdater", ProbeUpdater, create=True):
                message = asyncio.run(protocol.send(ui.UiRequest(
                    "plugin-search-request", "backtest.run",
                    "urn:lianghua:ui:input:backtest.run:v1", payload,
                )))[0]

            self.assertEqual(message.kind, "resource")
            value = message.data
            self.assertEqual(len(value["search"]["trials"]), 2)
            self.assertNotIn("validation", value["search"])
            self.assertEqual(value["parameters"], {"fast_window": 1, "slow_window": 3, "exposure": 0})
            self.assertEqual(value["search"]["best_run_id"], "plugin-search-trial-0002")
            identities = {ref.identity for ref in bus.list("runs")}
            self.assertTrue({
                "plugin-search", "plugin-search-trial-0001", "plugin-search-trial-0002",
            } <= identities)
            summary_manifest = json.loads(bus.read("runs", "plugin-search", "manifest.json"))
            saved_summary = json.loads(bus.read("runs", "plugin-search", "result.json"))
            from strategy import canonical_hash
            self.assertEqual(summary_manifest["parameters"], value["parameters"])
            self.assertEqual(summary_manifest["config_hash"], value["config_hash"])
            self.assertEqual(value["provenance"]["config_hash"], value["config_hash"])
            self.assertEqual(
                canonical_hash(value["provenance"]["effective_config"]),
                value["provenance"]["config_hash"],
            )
            self.assertEqual(
                canonical_hash(saved_summary["provenance"]["effective_config"]),
                saved_summary["provenance"]["config_hash"],
            )
            self.assertEqual(value["search"]["trials"][0]["trial_config_hash"],
                             json.loads(bus.read("runs", "plugin-search-trial-0001", "manifest.json"))["config_hash"])
            self.assertNotEqual(
                json.loads(bus.read("runs", "plugin-search-trial-0001", "manifest.json"))["config_hash"],
                summary_manifest["config_hash"],
            )
            runs = await_protocol(protocol, "research.runs", {})
            detail = await_protocol(protocol, "research.run", {"id": "plugin-search"})
            self.assertIn("plugin-search", {item["id"] for item in runs["items"]})
            self.assertEqual(detail["parameters"], value["parameters"])
            self.assertGreaterEqual(len(detail["evidence"]), 4)

    def test_search_rejects_fixed_validation_and_oos_fields_before_running(self):
        with TemporaryDirectory() as root:
            bus, protocol = self._fixture_protocol(root)
            for field in ("validation", "out_of_sample"):
                payload = self._search_payload(f"old-{field}", [
                    {"fast_window": 1, "slow_window": 3},
                ])
                payload[field] = {"start": "2020-02-04", "end": "2020-02-04"}
                message = asyncio.run(protocol.send(ui.UiRequest(
                    f"old-{field}-request", "backtest.run",
                    "urn:lianghua:ui:input:backtest.run:v1", payload,
                )))[0]
                self.assertEqual(message.kind, "problem")
                self.assertEqual(message.data["code"], "invalid_request")
            self.assertEqual(bus.list("runs"), ())

    def test_invalid_search_declaration_has_no_run_evidence(self):
        with TemporaryDirectory() as root:
            bus, protocol = self._fixture_protocol(root)
            payload = self._search_payload("parse-failure", [{"fast_window": 1, "slow_window": 3}])
            payload["search"]["updater"]["module"] = "math:ceil"
            message = asyncio.run(protocol.send(ui.UiRequest(
                "parse-failure-request", "backtest.run",
                "urn:lianghua:ui:input:backtest.run:v1", payload,
            )))[0]

            self.assertEqual(message.kind, "problem")
            self.assertEqual(message.data["code"], "invalid_search")
            self.assertEqual(bus.list("runs"), ())
            self.assertNotIn("extensions", message.data)

    def test_busy_does_not_write_failed_evidence(self):
        from protocol import BacktestError
        from market import DataBus
        from backend import KernelBacktestPort
        from protocol import parse_backtest_request
        from storage import DataStore

        class BusyKernel:
            def run(self, request):
                raise BacktestError("busy", "kernel already has an active run")

        with TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            port = KernelBacktestPort(bus, lambda *args, **kwargs: object())
            port._kernel = BusyKernel()
            with self.assertRaises(BacktestError):
                port.handle("backtest.run", {
                    "run_id": "busy", "module": "strategy", "params": {},
                    "dataset_id": "fixture", "start": "2020-01-01",
                    "end": "2020-01-01", "initial_cash": "100",
                })
            self.assertEqual(bus.list("runs"), ())

    def test_failed_second_trial_keeps_first_trial_and_failure_evidence(self):
        from market import DataBus
        from backend import KernelBacktestPort
        from storage import DataStore
        from tests.history_fixture import make_snapshot
        from tests.fee_fixture import configured_settlement
        from protocol import BacktestError

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, configured_settlement())
            payload = {
                "run_id": "failed-search", "module": "plugins.strategies.moving_average_cross",
                "params": {"fast_window": 1, "slow_window": 3}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-03", "initial_cash": "100000",
                "search": {
                    "updater": {
                        "module": "core.calculation:Calculation", "method": "propose",
                        "params": {"candidates": [
                            {"fast_window": 1, "slow_window": 3},
                            {"fast_window": 3, "slow_window": 3},
                        ]},
                    },
                    "max_trials": 2, "metric": "total_return", "direction": "max",
                },
            }
            with self.assertRaises(BacktestError) as caught:
                port.handle("backtest.run", payload)
            self.assertEqual(caught.exception.code, "search_failed")
            self.assertEqual(json.loads(bus.read("runs", "failed-search", "failed.json"))["partial"]["completed_trials"][0]["run_id"],
                             "failed-search-trial-0001")
            self.assertTrue(bus.path("runs", "failed-search-trial-0001", "manifest.json").exists())
            self.assertFalse(bus.path("runs", "failed-search", "manifest.json").exists())

    def test_protocol_rejects_external_updater_module_and_unknown_search_field(self):
        from protocol import parse_backtest_request
        from backend import _search

        base = {
            "run_id": "search", "module": "plugins.strategies.moving_average_cross",
            "params": {"fast_window": 1, "slow_window": 2}, "dataset_id": "fixture",
            "start": "2020-01-31", "end": "2020-02-03", "initial_cash": "100000",
            "search": {
                "updater": {"module": "math:ceil", "method": "x", "params": {}},
                "max_trials": 1, "metric": "total_return", "direction": "max",
            },
        }
        with self.assertRaises(Exception):
            _search(base, parse_backtest_request(base))
        with self.assertRaises(Exception):
            value = {**base, "search": {**base["search"], "extra": True}}
            _search(value, parse_backtest_request(value))
        with self.assertRaises(Exception):
            value = {**base, "search": {"updater": base["search"]["updater"],
                                         "max_trials": 1, "metric": "total_return"}}
            _search(value, parse_backtest_request(value))


if __name__ == "__main__":
    unittest.main()
