import asyncio
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.history_fixture import make_snapshot

ui = importlib.import_module("frontend.ui-protocol")


def send(protocol, request):
    return asyncio.run(protocol.send(request))[0]


EXECUTION = {
    "price": {"model": "ratio", "params": {"rate": "0.001", "tick": "0.01"}},
    "quantity": {"model": "capacity", "params": {}},
    "fees": {"model": "dated", "params": {"rounding": "none"}},
    "tax": {"model": "fifo", "params": {}},
    "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
}


def settlement_payload():
    return {"limits": {"lot_size": 100, "stock_weight": "0.4", "adv_fraction": "0.01"},
            "execution": EXECUTION}


def configured_plugin(initial_cash, fees, tax_policy, *, limits, execution):
    return (initial_cash, fees, tax_policy, limits, execution)


DATASET_VALIDATION_CALLS = []


def configured_plugin_validate_dataset(dataset, request, *, limits, execution):
    DATASET_VALIDATION_CALLS.append((dataset, request, limits, execution))


configured_plugin.validate_dataset = configured_plugin_validate_dataset
CONFIGURED_PLUGIN = f"{__name__}:configured_plugin"


class EmptyPort:
    def handle(self, operation, data):
        return {}


class ProtocolTests(unittest.TestCase):
    def test_execution_and_config_hashes_change_with_selected_execution(self):
        from datetime import datetime, timezone
        from decimal import Decimal
        from types import SimpleNamespace
        from protocal import BacktestRequest, build_effective_config, build_execution_hash

        request = BacktestRequest(
            "hash", "strategy", {}, "fixture",
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 2, tzinfo=timezone.utc), Decimal("100000"),
        )
        strategy = SimpleNamespace(params={})
        requirements = SimpleNamespace(fields=("close",), history_bars=1)
        dataset = SimpleNamespace(fields=("close",), history=1)
        provenance = {"data_hash": "data", "source_hashes": {"bars": "source"}, "code_hash": "code"}
        first = {"execution": EXECUTION}
        second = {"execution": {**EXECUTION, "price": {
            "model": "ratio", "params": {"rate": "0.002", "tick": "0.01"},
        }}}

        _, first_config_hash = build_effective_config(request, strategy, requirements, dataset, first)
        _, second_config_hash = build_effective_config(request, strategy, requirements, dataset, second)
        self.assertNotEqual(build_execution_hash(provenance, first), build_execution_hash(provenance, second))
        self.assertNotEqual(first_config_hash, second_config_hash)

    def test_execution_validation_is_shared_by_request_and_config_paths(self):
        from protocal import BacktestError, _load_plugin, parse_backtest_request

        invalid = {**EXECUTION,
                   "price": {"model": "ratio", "params": {"rate": "1", "tick": "0.01"}}}
        payload = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000", "execution": invalid,
        }
        with self.assertRaises(BacktestError):
            parse_backtest_request(payload)

        with self.assertRaises(ValueError):
            _load_plugin({"settlement": {"module": CONFIGURED_PLUGIN, "params": {
                "limits": {"lot_size": 100}, "execution": invalid,
            }}}, "settlement")

        configured = _load_plugin({"settlement": {"module": CONFIGURED_PLUGIN, "params": {
            "limits": {"lot_size": 100}, "execution": EXECUTION,
        }}}, "settlement")
        self.assertEqual(configured.describe({})["params"]["execution"]["quantity"]["params"], {})

    def test_loaded_plugin_exposes_one_dataset_validation_hook_with_merged_request(self):
        from types import SimpleNamespace
        from protocal import _load_plugin

        DATASET_VALIDATION_CALLS.clear()
        configured = _load_plugin({"settlement": {"module": CONFIGURED_PLUGIN, "params": {
            "limits": {"lot_size": 100}, "execution": EXECUTION,
        }}}, "settlement")
        dataset = object()
        request = SimpleNamespace(settlement_parameters={
            "limits": {"lot_size": 200}, "execution": EXECUTION,
        })

        configured.validate_dataset(dataset, request, limits={"lot_size": 300})

        self.assertEqual(len(DATASET_VALIDATION_CALLS), 1)
        actual_dataset, actual_request, limits, execution = DATASET_VALIDATION_CALLS[0]
        self.assertIs(actual_dataset, dataset)
        self.assertIs(actual_request, request)
        self.assertEqual(limits.lot_size, 300)
        self.assertEqual(execution["quantity"]["params"], {})

    def test_execution_rules_reject_invalid_bounds_and_mismatched_ticks(self):
        from protocal import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000",
        }
        for execution in (
            {**EXECUTION, "quantity": {"model": "participation", "params": {"participation": "1.1", "capacity_basis": "prior20"}}},
            {**EXECUTION, "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.02"}}},
        ):
            with self.subTest(execution=execution), self.assertRaises(BacktestError):
                parse_backtest_request({**base, "execution": execution})

    def test_standard_request_keeps_strict_execution_actions(self):
        from protocal import parse_backtest_request

        execution = {
            "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.01"}},
            "quantity": {"model": "participation", "params": {"participation": "0.2", "capacity_basis": "prior20"}},
            "fees": {"model": "dated", "params": {"rounding": "half_up"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }
        request = parse_backtest_request({
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000", "execution": execution,
        })

        self.assertEqual(request.settlement_parameters["execution"], execution)

    def test_standard_request_rejects_old_execution_names_and_unknown_keys(self):
        from protocal import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000",
        }
        old = {**base, "execution": {
            "price": {"model": "ratio", "params": {"ratio": "0.01", "tick": "0.01"}},
            "quantity": {"model": "cash_capacity", "params": {}},
            "fees": {"model": "dated", "params": {}},
            "tax": {"model": "fifo_natural", "params": {}},
            "check": {"model": "explicit", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(old)

        unknown = {**base, "execution": {
            "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.01", "extra": 1}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(unknown)

        malformed = {**base, "execution": {
            "price": {"model": "ratio", "params": {"rate": True, "tick": "0.01"}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(malformed)

    def test_kernel_result_is_saved_once_in_the_shared_data_store(self):
        from protocal import DataBus, KernelBacktestPort
        from storage import DataStore
        from tests.fee_fixture import configured_settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, configured_settlement())
            result = port.handle("workbench.backtest", {
                "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                **settlement_payload(),
            })
            saved = json.loads(bus.read("runs", "one", "result.json"))

        self.assertEqual(result.data["run_id"], "one")
        self.assertEqual(saved["trace"], result.data["trace"])
        self.assertEqual(saved["equity"], result.data["equity"])

    def test_protocol_routes_backtest_to_the_injected_port(self):
        from protocal import DataBus, KernelBacktestPort, Modules, Protocol
        from storage import DataStore
        from tests.fee_fixture import configured_settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            backend = Protocol(Modules(
                KernelBacktestPort(bus, configured_settlement()), *[EmptyPort() for _ in range(4)]))
            message = send(backend, ui.UiRequest(
                "one", "workbench.backtest", "urn:lianghua:ui:input:workbench.backtest:v1", {
                    "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                    "params": {"positions": 4}, "dataset_id": "fixture",
                    "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                    **settlement_payload(),
                }))

        wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))
        self.assertEqual(message.kind, "resource")
        self.assertEqual(wire["data"]["run_id"], "one")
        self.assertIn("provenance", wire["data"])
