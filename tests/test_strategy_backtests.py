from pathlib import Path
import importlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from strategy import load_strategy
from protocal import build_data
from protocal import KernelBacktestPort


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
        data = build_data(self.root)
        self.port = KernelBacktestPort(data, lambda *args, **kwargs: object(), config=config,
                                       research=EmptyResearch())
        self.request = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "initial_cash": "100000.00",
            "start": "2020-01-01", "end": "2020-12-31", "dataset_id": "fixture",
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
        self.assertIn(
            "plugins.strategies.breadth_low_amount_reversal",
            response["availability"]["requirements"],
        )
        self.assertNotIn("公司行动", response["availability"]["issues"][0]["message"])

    def test_catalog_keeps_fixed_slot_default(self):
        item = next(
            value for value in self.port.handle("workbench.strategies", {})["items"]
            if value["module"] == "plugins.strategies.breadth_low_amount_reversal"
        )
        self.assertEqual(item["defaults"]["positions"], 8)
        self.assertEqual(
            next(value for value in item["parameters"] if value["key"] == "positions")["type"],
            "integer",
        )

    def test_preflight_has_effective_parameters_requirements_and_hash(self):
        first = self.port.handle("workbench.preflight", self.request)
        second = self.port.handle(
            "workbench.preflight", {**self.request, "params": {"trend_window": 160}}
        )
        self.assertFalse(first["ready"])
        self.assertIsNone(first["config_hash"])
        self.assertIsNone(second["config_hash"])
        self.assertNotEqual(
            first["requirements"]["history_bars"], second["requirements"]["history_bars"]
        )
        self.assertEqual(first["effective_params"]["positions"], 8)

    def test_preflight_accepts_the_public_decimal_string(self):
        result = self.port.handle(
            "workbench.preflight", {**self.request, "initial_cash": "100000.00"}
        )
        self.assertEqual(result["effective_config"]["initial_cash"], "100000.00")

    def test_preflight_uses_the_protocol_request_contract(self):
        result = self.port.handle("workbench.preflight", self.request)
        self.assertIn("requirements", result)

    def test_preflight_reports_unavailable_history_as_a_public_problem(self):
        result = self.port.handle("workbench.preflight", self.request)
        self.assertFalse(result["ready"])
        self.assertTrue(result["issues"])

    def test_wrong_parameters_fail_before_history(self):
        for params in ({"positions": "8"}, {"positions": True}, {"wrong": 3}):
            with self.subTest(params=params):
                result = self.port.handle(
                    "workbench.preflight", {**self.request, "params": params}
                )
                self.assertFalse(result["ready"])
                self.assertTrue(result["issues"])

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
                result = self.port.handle(
                    "workbench.preflight", {**self.request, "module": name}
                )
                self.assertFalse(result["ready"])
                self.assertTrue(result["issues"])
                self.assertEqual(
                    self.port.handle("workbench.strategies", {})["items"], []
                )
        finally:
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
