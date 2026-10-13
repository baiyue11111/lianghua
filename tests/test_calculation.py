from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest

from protocal import BacktestError, BacktestRequest, save_backtest
from core.calculation import Calculation


class CalculationTests(unittest.TestCase):
    def test_sma_uses_the_declared_window(self):
        self.assertEqual(Calculation.sma([1, 2, 3], 2), 2.5)

    def test_sma_does_not_revalidate_values_before_the_declared_window(self):
        self.assertEqual(Calculation.sma([float("nan"), 1, 2], 2), 1.5)
        with self.assertRaises(ValueError):
            Calculation.sma([1, float("nan"), 2], 2)

    def test_sma_rejects_finite_inputs_that_overflow_result(self):
        with self.assertRaises(ValueError):
            Calculation.sma([1e308, 1e308], 2)

    def test_equal_weights_keeps_slots_as_the_denominator(self):
        self.assertEqual(Calculation.equal_weights(("A", "B"), 8), {"A": .125, "B": .125})

    def test_equal_weights_allows_explicit_empty_selection(self):
        self.assertEqual(Calculation.equal_weights((), 8), {})

    def test_evaluate_keeps_default_metrics(self):
        metrics = Calculation.evaluate([
            (date(2024, 1, 2), 100.0),
            (date(2024, 1, 3), 120.0),
            (date(2024, 1, 4), 90.0),
        ])
        self.assertEqual(metrics["observations"], 3)
        self.assertAlmostEqual(metrics["total_return"], -.1)
        self.assertAlmostEqual(metrics["max_drawdown"], -.25)

    def test_evaluate_baseline_changes_only_excess_return_sharpe(self):
        points = [
            (date(2024, 1, 1), 100.0),
            (date(2024, 1, 2), 110.0),
            (date(2024, 1, 3), 99.0),
        ]
        base = Calculation.evaluate(points, periods=12, baseline=0)
        adjusted = Calculation.evaluate(
            points,
            periods=12,
            baseline=.02,
        )
        self.assertEqual(adjusted["total_return"], base["total_return"])
        self.assertEqual(adjusted["cagr"], base["cagr"])
        self.assertEqual(adjusted["annualized_volatility"], base["annualized_volatility"])
        self.assertNotEqual(adjusted["sharpe"], base["sharpe"])

    def test_evaluate_periods_changes_annualized_metrics(self):
        points = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 110.0), (date(2024, 1, 3), 99.0)]
        short = Calculation.evaluate(points, periods=12, baseline=.02)
        long = Calculation.evaluate(points, periods=24, baseline=.02)
        self.assertNotEqual(short["cagr"], long["cagr"])
        self.assertNotEqual(short["annualized_volatility"], long["annualized_volatility"])

    def test_calculation_rejects_invalid_values(self):
        cases = (
            lambda: Calculation.sma([1], 2),
            lambda: Calculation.sma([True, 2], 2),
            lambda: Calculation.evaluate([(date(2024, 1, 1), 100.0), (date(2024, 1, 2), float("nan"))]),
            lambda: Calculation.evaluate([(date(2024, 1, 2), 100.0), (date(2024, 1, 1), 101.0)]),
            lambda: Calculation.evaluate([(date(2024, 1, 1), 0.0)]),
            lambda: Calculation.evaluate([(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 101.0)], periods=True),
            lambda: Calculation.evaluate([(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 101.0)], baseline=True),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises((TypeError, ValueError)):
                    case()

    def test_evaluate_rejects_finite_inputs_that_overflow_metrics(self):
        with self.assertRaises(ValueError):
            Calculation.evaluate([
                (date(2024, 1, 1), 1e-308),
                (date(2024, 1, 2), 1e308),
            ])

    def test_request_freezes_evaluation_parameters_and_rejects_unknown_values(self):
        request = BacktestRequest(
            "run", "strategy", {}, "dataset",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            Decimal("100"), evaluation_parameters={"periods": 12, "baseline": .01},
        )
        self.assertEqual(dict(request.evaluation_parameters), {"periods": 12, "baseline": .01})
        with self.assertRaises(BacktestError):
            BacktestRequest(
                "run", "strategy", {}, "dataset",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                Decimal("100"), evaluation_parameters={"unknown": 1},
            )

    def test_save_backtest_reuses_result_effective_config(self):
        class Data:
            def __init__(self):
                self.writes = {}

            def write(self, kind, identity, name, value):
                self.writes[(kind, identity, name)] = value

        data = Data()
        request = BacktestRequest(
            "run", "strategy", {}, "dataset",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            Decimal("100"), evaluation_parameters={"periods": 12, "baseline": .01},
        )
        value = {
            "metrics": {"total_return": .1},
            "effective_config": {
                "dataset_id": "dataset", "evaluation": {"periods": 12, "baseline": .01},
                "settlement": {"module": "plugins.data:Settlement", "params": {"limits": {"lot_size": 100}}},
                "signal_plugins": {"scaler": {"method": "standard"}},
            },
            "config_hash": "hash", "provenance": {}, "trace": {},
        }
        save_backtest(data, request, value)
        manifest = json.loads(data.writes[("runs", "run", "manifest.json")])
        self.assertEqual(manifest["effective_config"], value["effective_config"])
        self.assertEqual(manifest["config_hash"], value["config_hash"])


if __name__ == "__main__":
    unittest.main()
