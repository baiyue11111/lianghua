from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest

from protocol import BacktestError, BacktestRequest
from protocol import HistoryBar, MarketFrame
from backend import save_backtest
from core.calculation import Calculation


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def test_fundamentals_uses_signed_population_z_scores(self):
        frame = MarketFrame(self.at, "CN_EQUITY", {
            "A": {"quality": 1, "value": 4},
            "B": {"quality": 3, "value": 2},
        }, {"source": "fixture"})
        result = Calculation.fundamentals(frame, {"quality": 2, "value": -1})
        self.assertEqual(list(result.fields), ["A", "B"])
        self.assertAlmostEqual(result.fields["A"]["quality"], -1.0)
        self.assertAlmostEqual(result.fields["A"]["value"], 1.0)
        self.assertAlmostEqual(result.fields["A"]["score"], -3.0)
        self.assertAlmostEqual(result.fields["B"]["quality"], 1.0)
        self.assertAlmostEqual(result.fields["B"]["value"], -1.0)
        self.assertAlmostEqual(result.fields["B"]["score"], 3.0)
        self.assertEqual(result.at, frame.at)
        self.assertEqual(result.venue, frame.venue)
        self.assertEqual(dict(result.context), dict(frame.context))
        self.assertEqual(dict(result.history), {})

    def test_fundamentals_keeps_missing_values_and_constant_columns(self):
        frame = MarketFrame(self.at, "CN_EQUITY", {
            "A": {"x": 1, "flat": 5},
            "B": {"x": None, "flat": 5},
            "C": {"x": 3, "flat": None},
        })
        result = Calculation.fundamentals(frame, {"x": 1, "flat": 2})
        self.assertEqual([result.fields[s]["flat"] for s in ("A", "B", "C")], [0.0, 0.0, None])
        self.assertAlmostEqual(result.fields["A"]["score"], -1.0)
        self.assertIsNone(result.fields["B"]["score"])
        self.assertIsNone(result.fields["C"]["score"])

    def test_fundamentals_keeps_all_missing_and_empty_universes(self):
        frame = MarketFrame(self.at, "CN_EQUITY", {"A": {"x": None}, "B": {"x": None}})
        result = Calculation.fundamentals(frame, {"x": 1})
        self.assertEqual([result.fields[s]["x"] for s in ("A", "B")], [None, None])
        self.assertEqual([result.fields[s]["score"] for s in ("A", "B")], [None, None])
        empty = Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {}), {"x": 1})
        self.assertEqual(dict(empty.fields), {})

    def test_fundamentals_validates_weights_and_factor_values(self):
        frame = MarketFrame(self.at, "CN_EQUITY", {"A": {"x": 1}})
        invalid_weights = (
            {}, [], {"": 1}, {"x": 0}, {"x": True}, {"x": float("inf")},
            {"score": 1}, {"y": 1},
        )
        for weights in invalid_weights:
            with self.subTest(weights=weights), self.assertRaises((TypeError, ValueError)):
                Calculation.fundamentals(frame, weights)
        for value in (True, "1", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {"A": {"x": value}}), {"x": 1})
        with self.assertRaisesRegex(ValueError, "field 'x'.*security 'A'"):
            Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {"A": {}}), {"x": 1})
        with self.assertRaisesRegex(ValueError, "field 'x'.*security 'A'"):
            Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {"A": {"x": 10 ** 1000}}), {"x": 1})
        overflow = MarketFrame(self.at, "CN_EQUITY", {
            "A": {"x": -1}, "B": {"x": 0}, "C": {"x": 1},
        })
        with self.assertRaisesRegex(ValueError, "score overflow.*security 'A'.*field 'x'"):
            Calculation.fundamentals(overflow, {"x": 1.7e308})

    def test_fundamentals_is_order_invariant_and_does_not_mutate_input_or_history(self):
        history = {"A": (HistoryBar(self.at, {"x": 99}),)}
        frame = MarketFrame(self.at, "CN_EQUITY", {"A": {"x": 1}, "B": {"x": 2}}, history=history)
        before = {symbol: dict(fields) for symbol, fields in frame.fields.items()}
        result = Calculation.fundamentals(frame, {"x": 3})
        reverse = Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {"B": {"x": 2}, "A": {"x": 1}}), {"x": 3})
        scaled = Calculation.fundamentals(MarketFrame(self.at, "CN_EQUITY", {"A": {"x": 10}, "B": {"x": 20}}), {"x": 3})
        self.assertEqual(list(result.fields), ["A", "B"])
        self.assertEqual(result.fields["A"]["score"], reverse.fields["A"]["score"])
        self.assertEqual(result.fields["A"]["score"], scaled.fields["A"]["score"])
        self.assertEqual({symbol: dict(fields) for symbol, fields in frame.fields.items()}, before)
        self.assertEqual(len(frame.history["A"]), 1)
        self.assertEqual(dict(result.history), {})

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

    def test_evaluate_risk_free_rate_changes_only_excess_return_sharpe(self):
        points = [
            (date(2024, 1, 1), 100.0),
            (date(2024, 1, 2), 110.0),
            (date(2024, 1, 3), 99.0),
        ]
        base = Calculation.evaluate(points, periods=12, risk_free_rate=0)
        adjusted = Calculation.evaluate(
            points,
            periods=12,
            risk_free_rate=.02,
        )
        self.assertEqual(adjusted["total_return"], base["total_return"])
        self.assertEqual(adjusted["cagr"], base["cagr"])
        self.assertEqual(adjusted["annualized_volatility"], base["annualized_volatility"])
        self.assertNotEqual(adjusted["sharpe"], base["sharpe"])

    def test_annual_risk_free_rate_is_converted_to_period_rate(self):
        points = [
            (date(2024, 1, 1), 100.0),
            (date(2024, 1, 2), 110.0),
            (date(2024, 1, 3), 99.0),
        ]
        per_period = 1.05 ** (1 / 12) - 1
        expected = (((0.1 - per_period) + (-0.1 - per_period)) / 2) / (0.02 ** 0.5) * (12 ** 0.5)
        result = Calculation.evaluate(points, periods=12, risk_free_rate=0.05)
        self.assertAlmostEqual(result["sharpe"], expected, places=12)

    def test_evaluate_periods_changes_annualized_metrics(self):
        points = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 110.0), (date(2024, 1, 3), 99.0)]
        short = Calculation.evaluate(points, periods=12, risk_free_rate=.02)
        long = Calculation.evaluate(points, periods=24, risk_free_rate=.02)
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
            lambda: Calculation.evaluate([(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 101.0)], risk_free_rate=True),
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
            Decimal("100"), evaluation_parameters={"periods": 12, "risk_free_rate": .01},
        )
        self.assertEqual(dict(request.evaluation_parameters), {"periods": 12, "risk_free_rate": .01})
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
            Decimal("100"), evaluation_parameters={"periods": 12, "risk_free_rate": .01},
        )
        value = {
            "metrics": {"total_return": .1},
            "effective_config": {
                "dataset_id": "dataset", "evaluation": {"periods": 12, "risk_free_rate": .01},
                "settlement": {"module": "plugins.data:Settlement", "params": {"limits": {}}},
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
