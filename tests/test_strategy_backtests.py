from pathlib import Path
import importlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from strategy import load_strategy
from protocal import build_data
from workbench.port import WorkbenchPort


class EmptyResearch:
    def list(self):
        return []

    def detail(self, *args):
        raise KeyError()


class BacktestPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = {"sources": [], "backtests": {}}
        from collect.data import Store
        data = build_data(self.root)
        self.port = WorkbenchPort(self.root, config, data, store=Store(data), research=EmptyResearch())
        self.request = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "initial_cash": "100000.00",
            "start": "2020-01-01", "end": "2020-12-31", "dataset_id": "",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_uses_declared_parameter_schema(self):
        response = self.port.handle("workbench.strategies", {})
        item = response["items"][0]
        self.assertEqual(item["defaults"]["positions"], 8)
        self.assertEqual(
            next(value for value in item["parameters"] if value["key"] == "positions")["type"],
            "integer",
        )
        self.assertFalse(response["availability"]["ready"])

    def test_preflight_has_effective_parameters_requirements_and_hash(self):
        first = self.port.handle("workbench.preflight", self.request)
        second = self.port.handle(
            "workbench.preflight", {**self.request, "params": {"trend_window": 160}}
        )
        self.assertFalse(first["ready"])
        self.assertNotEqual(first["config_hash"], second["config_hash"])
        self.assertNotEqual(
            first["requirements"]["history_bars"], second["requirements"]["history_bars"]
        )

    def test_preflight_accepts_the_public_decimal_string(self):
        result = self.port.handle(
            "workbench.preflight", {**self.request, "initial_cash": "100000.00"}
        )
        self.assertEqual(result["effective_config"]["initial_cash"], 100000.0)

    def test_wrong_parameters_fail_before_history(self):
        for params in ({"positions": "8"}, {"positions": True}, {"wrong": 3}):
            with self.subTest(params=params), self.assertRaises((TypeError, ValueError)):
                self.port.handle("workbench.preflight", {**self.request, "params": params})

    def test_removed_cached_plugin_is_rejected_before_data_checks(self):
        package = importlib.import_module("plugins.strategies")
        importlib.import_module("plugins.strategies.moving_average_cross")
        name = "plugins.strategies.removal_probe_api"
        source = self.root / "removal_probe_api.py"
        source.write_text(
            "from plugins.strategies.moving_average_cross import NAME, Params, Strategy\n"
        )
        try:
            with patch.object(package, "__path__", [str(self.root)]):
                load_strategy(name, {})
                source.unlink()
                with self.assertRaisesRegex(ValueError, "策略文件已移除或不存在"):
                    self.port.handle(
                        "workbench.preflight", {**self.request, "module": name}
                    )
                self.assertEqual(
                    self.port.handle("workbench.strategies", {})["items"], []
                )
        finally:
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
