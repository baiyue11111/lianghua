from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


class RemoveOldContractsTests(unittest.TestCase):
    def test_protocol_import_has_no_outer_runtime_side_effects(self):
        code = (
            "import json, sys; import protocal; "
            "print(json.dumps({name: name in sys.modules for name in "
            "('core.kernel', 'application', 'frontend.ui-protocol')}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"core.kernel": False, "application": False, "frontend.ui-protocol": False},
        )

    def test_public_contracts_are_defined_once_in_protocol(self):
        protocol = importlib.import_module("protocal")
        strategy = importlib.import_module("plugins.strategies.moving_average_cross")
        account_execution = importlib.import_module("account_execution")
        account_ledger = importlib.import_module("account_ledger")
        execution = importlib.import_module("execution")

        self.assertIs(strategy.DataRequest, protocol.DataRequest)
        self.assertIs(strategy.TargetSet, protocol.TargetSet)
        self.assertIs(account_execution.Bar, protocol.Bar)
        self.assertIs(account_execution.Order, protocol.Order)
        self.assertIs(account_execution.Fill, protocol.Fill)
        self.assertIs(account_ledger.Order, protocol.Order)
        self.assertIs(account_ledger.Fill, protocol.Fill)
        self.assertIs(execution.Bar, protocol.Bar)
        self.assertIs(execution.Order, protocol.Order)
        self.assertIs(execution.Fill, protocol.Fill)

    def test_data_port_declares_bus_operations(self):
        protocol = importlib.import_module("protocal")
        members = protocol.DataPort.__dict__
        self.assertIn("query", members)
        self.assertIn("read_ref", members)
        self.assertNotIn("_DataStore", protocol.__all__)

    def test_fixed_kernel_has_no_run_or_components_export(self):
        kernel = importlib.import_module("core.kernel")
        self.assertFalse(hasattr(kernel, "Run"))
        self.assertFalse(hasattr(kernel, "Components"))

    def test_retired_modules_are_not_importable(self):
        names = (
            "contracts",
            "core.contracts",
            "core.backtest",
            "core.interfaces",
            "account_history",
            "research",
            "strategies",
        )
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)
        self.assertFalse((Path(__file__).parents[1] / "tools/run_backtest.py").exists())

    def test_history_zero_records_dataset_history_in_effective_config(self):
        from protocal import BacktestRequest, BacktestResponse, _result_data

        request = BacktestRequest(
            run_id="history-zero",
            strategy_id="plugins.strategies.moving_average_cross",
            parameters={"fast_window": 20, "slow_window": 60},
            dataset_id="fixture",
            start=datetime(2024, 1, 2, tzinfo=UTC),
            end=datetime(2024, 12, 31, tzinfo=UTC),
            initial_cash=Decimal("1000000"),
            history=0,
        )
        dataset = SimpleNamespace(
            fields=("open", "close"),
            history=60,
            provenance={},
            scope={"symbols": ("600036",), "universe": "fixed_symbols"},
        )
        result = BacktestResponse(
            request.run_id,
            request.strategy_id,
            request.dataset_id,
            request.start,
            request.end,
            1,
            1,
            Decimal("1000000"),
            Decimal("1000000"),
            {},
            0,
            dataset=dataset,
        )

        value = _result_data(result, request)

        self.assertEqual(value["effective_config"]["history"], 60)
        self.assertEqual(value["config_hash"], _result_data(result, request)["config_hash"])

    def test_prepare_requires_explicit_policy_source_before_output_creation(self):
        path = Path(__file__).parents[1] / "tools/prepare_history.py"
        spec = importlib.util.spec_from_file_location("prepare_history", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, "account policy source"):
                module.prepare(root, {"dataset_id": "out"})
            self.assertFalse((root / "data/sources/out").exists())


if __name__ == "__main__":
    unittest.main()
