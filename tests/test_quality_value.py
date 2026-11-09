"""Behavior tests for the quality, growth, and value strategy."""
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch

from core.calculation import Calculation
from protocol import AccountSnapshot, HistoryBar, MarketFrame
from strategy import load_strategy


MODULE = "plugins.strategies.quality_value"


class QualityValueTests(unittest.TestCase):
    def strategy(self, **params):
        return load_strategy(MODULE, params)

    def frame(self, rows, *, day="2026-06-30", following="2026-07-01", trading=True,
              history_counts=None, history_values=None, history_signals=None,
              history_amounts=None, history_volumes=None):
        at = datetime.fromisoformat(day + "T15:00:00").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        history_counts = history_counts or {}
        history_values = history_values or {}
        history_signals = history_signals or {}
        history_amounts = history_amounts or {}
        history_volumes = history_volumes or {}
        history = {
            symbol: self.history(
                at,
                count=history_counts.get(symbol, 20),
                amount=history_values.get(symbol, (1000.0, 100.0))[0],
                volume=history_values.get(symbol, (1000.0, 100.0))[1],
                signals=history_signals.get(symbol),
                amounts=history_amounts.get(symbol),
                volumes=history_volumes.get(symbol),
            )
            for symbol in rows
        }
        return MarketFrame(at, "CN_EQUITY", rows,
                           {"trading": trading, "next_session": following}, history)

    def history(self, at, *, count=20, amount=1000.0, volume=100.0, signals=None,
                amounts=None, volumes=None):
        days = []
        day = at.date()
        while len(days) < count:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.reverse()
        signals = signals if signals is not None else [10.0] * count
        if len(signals) != count:
            raise ValueError("signals must match count")
        amounts = amounts if amounts is not None else [amount] * count
        volumes = volumes if volumes is not None else [volume] * count
        return tuple(
            HistoryBar(datetime.combine(day, time(15), tzinfo=at.tzinfo),
                       {"amount": amt, "volume": vol, "signal_close": signal})
            for day, signal, amt, vol in zip(days, signals, amounts, volumes)
        )

    def row(self, **values):
        return {"close": 10.0, "book_value": 10.0, "roe": 0.1,
                "profit_growth": 0.1} | values

    def generate(self, strategy, frame):
        return strategy.generate(
            frame, AccountSnapshot(frame.at, Decimal("1000"), Decimal("1000"), {})
        )

    def test_composite_rank_selects_highest_quality_growth_and_value_score(self):
        strategy = self.strategy(positions=1)
        rows = {
            "A": self.row(roe=0.5, profit_growth=0.1, book_value=2.0),
            "B": self.row(roe=0.2, profit_growth=0.5, book_value=1.0),
            "C": self.row(roe=0.1, profit_growth=0.2, book_value=3.0),
            "D": self.row(roe=0.1, profit_growth=0.1, book_value=1.0),
        }
        target = self.generate(strategy, self.frame(rows))
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_default_ranking_matches_explicit_score(self):
        rows = {
            "A": self.row(roe=0.5, profit_growth=0.1, book_value=2.0),
            "B": self.row(roe=0.2, profit_growth=0.5, book_value=1.0),
            "C": self.row(roe=0.1, profit_growth=0.2, book_value=3.0),
        }
        frame = self.frame(rows)
        default = self.generate(self.strategy(), frame)
        explicit = self.generate(self.strategy(ranking="score"), frame)
        self.assertEqual(default, explicit)

    def test_ranking_rejects_unknown_option(self):
        with self.assertRaises(ValueError):
            self.strategy(ranking="unknown")

    def test_growth_field_controls_required_data(self):
        request = self.strategy(growth_field="revenue_growth").requirements()
        self.assertEqual(
            request.fields,
            ("close", "book_value", "roe", "revenue_growth", "amount", "volume"),
        )

    def test_selected_growth_field_is_required(self):
        row = self.row(revenue_growth=0.2)
        del row["revenue_growth"]
        with self.assertRaises(KeyError):
            self.generate(
                self.strategy(growth_field="revenue_growth", ranking="rank"),
                self.frame({"A": row}),
            )

    def test_growth_field_rejects_unknown_option(self):
        with self.assertRaises(ValueError):
            self.strategy(growth_field="net_profit")

    def test_default_growth_field_matches_explicit_profit_growth(self):
        rows = {
            "A": self.row(roe=0.5, profit_growth=0.1, book_value=2.0),
            "B": self.row(roe=0.2, profit_growth=0.5, book_value=1.0),
            "C": self.row(roe=0.1, profit_growth=0.2, book_value=3.0),
        }
        frame = self.frame(rows)
        default = self.generate(self.strategy(ranking="rank"), frame)
        explicit = self.generate(
            self.strategy(ranking="rank", growth_field="profit_growth"), frame
        )
        self.assertEqual(default, explicit)

    def test_growth_field_changes_selection_and_ignores_the_other_growth_field(self):
        rows = {
            "A": self.row(roe=0.2, book_value=2.0, profit_growth=10.0, revenue_growth=0.0),
            "B": self.row(roe=0.2, book_value=2.0, profit_growth=0.0, revenue_growth=10.0),
            "C": self.row(roe=0.1, book_value=1.0, profit_growth=5.0, revenue_growth=5.0),
        }
        profit_rows = {symbol: dict(row) for symbol, row in rows.items()}
        profit_rows["A"]["revenue_growth"] = 1e300
        profit_rows["B"]["revenue_growth"] = -1e300
        revenue_rows = {symbol: dict(row) for symbol, row in rows.items()}
        revenue_rows["A"]["profit_growth"] = -1e300
        revenue_rows["B"]["profit_growth"] = 1e300

        profit = self.generate(
            self.strategy(positions=1, ranking="rank", growth_field="profit_growth"),
            self.frame(profit_rows),
        )
        revenue = self.generate(
            self.strategy(positions=1, ranking="rank", growth_field="revenue_growth"),
            self.frame(revenue_rows),
        )
        self.assertEqual(dict(profit.weights), {"A": 1.0})
        self.assertEqual(dict(revenue.weights), {"B": 1.0})

    def test_rank_is_forwarded_and_extreme_growth_keeps_selection_when_order_is_same(self):
        strategy = self.strategy(positions=1, ranking="rank")
        ordinary = {
            "A": self.row(roe=0.1, profit_growth=1.0, book_value=1.0),
            "B": self.row(roe=0.2, profit_growth=2.0, book_value=2.0),
            "C": self.row(roe=0.3, profit_growth=3.0, book_value=3.0),
            "D": self.row(roe=0.4, profit_growth=4.0, book_value=4.0),
        }
        extreme = {symbol: dict(row) for symbol, row in ordinary.items()}
        extreme["D"]["profit_growth"] = 1e300
        with patch.object(Calculation, "fundamentals", wraps=Calculation.fundamentals) as call:
            first = self.generate(strategy, self.frame(ordinary))
            second = self.generate(strategy, self.frame(extreme))
        self.assertEqual([item.kwargs["ranking"] for item in call.call_args_list], ["rank", "rank"])
        self.assertEqual(dict(first.weights), dict(second.weights))

    def test_month_inside_period_does_not_trade_and_each_month_end_rebalances(self):
        strategy = self.strategy()
        rows = {"A": self.row(), "B": self.row(roe=0.2)}
        inside = self.frame(rows, day="2026-07-02", following="2026-07-03")
        self.assertIsNone(self.generate(strategy, inside))

        june = self.generate(strategy, self.frame(rows))
        july = self.generate(
            strategy,
            self.frame(rows, day="2026-07-31", following="2026-08-03"),
        )
        self.assertIsNotNone(june)
        self.assertIsNotNone(july)
        self.assertEqual(dict(june.weights), dict(july.weights))

    def test_nontrading_phase_does_not_emit_a_target(self):
        target = self.generate(
            self.strategy(), self.frame({"A": self.row()}, trading=False)
        )
        self.assertIsNone(target)

    def test_empty_month_end_pool_emits_empty_target_to_clear_holdings(self):
        strategy = self.strategy()
        first = self.generate(strategy, self.frame({"A": self.row()}))
        empty = self.generate(
            strategy,
            self.frame({"A": self.row(roe=-0.1)}, day="2026-07-31", following="2026-08-03"),
        )
        self.assertEqual(dict(first.weights), {"A": 1.0})
        self.assertEqual(dict(empty.weights), {})

    def test_missing_none_fields_exclude_security_without_filling_zero(self):
        rows = {
            "A": self.row(roe=0.9, profit_growth=0.9, book_value=100.0, close=1.0),
            "B": self.row(roe=0.2),
        }
        rows["A"]["profit_growth"] = None
        target = self.generate(self.strategy(), self.frame(rows))
        self.assertEqual(dict(target.weights), {"B": 1.0})

    def test_missing_field_key_is_rejected_instead_of_treated_as_none(self):
        row = self.row()
        del row["profit_growth"]
        with self.assertRaises(KeyError):
            self.generate(self.strategy(), self.frame({"A": row}))

    def test_only_old_stocks_with_20_positive_amount_and_volume_bars_qualify(self):
        rows = {symbol: self.row() for symbol in
                ("IPO", "ZERO_AMOUNT", "NONE_AMOUNT", "ZERO_VOLUME", "NONE_VOLUME", "OLD")}
        frame = self.frame(
            rows,
            history_counts={"IPO": 19},
            history_values={
                "ZERO_AMOUNT": (0.0, 100.0),
                "NONE_AMOUNT": (None, 100.0),
                "ZERO_VOLUME": (1000.0, 0.0),
                "NONE_VOLUME": (1000.0, None),
            },
        )
        target = self.generate(self.strategy(), frame)
        self.assertEqual(dict(target.weights), {"OLD": 1.0})

    def test_non_numeric_or_non_finite_fields_are_rejected(self):
        strategy = self.strategy()
        for field in ("close", "book_value", "roe", "profit_growth"):
            for value in (True, "bad", float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.generate(
                            strategy, self.frame({"A": self.row(**{field: value})})
                        )

    def test_equal_scores_use_code_order_and_equal_weights(self):
        rows = {code: self.row() for code in ("C", "A", "B")}
        target = self.generate(self.strategy(positions=2), self.frame(rows))
        self.assertEqual(dict(target.weights), {"A": 0.5, "B": 0.5})

    def test_strategy_does_not_mutate_input_rows_or_context(self):
        rows = {"A": self.row(), "B": self.row(roe=0.2)}
        before_rows = deepcopy(rows)
        frame = self.frame(rows)
        before_context = dict(frame.context)
        self.generate(self.strategy(), frame)
        self.assertEqual(rows, before_rows)
        self.assertEqual(dict(frame.context), before_context)

    def test_requirements_include_twenty_liquidity_sessions(self):
        request = self.strategy().requirements()
        self.assertEqual(request.fields,
                         ("close", "book_value", "roe", "profit_growth", "amount", "volume"))
        self.assertEqual(request.history_bars, 20)

    def test_default_trend_keeps_requirements_and_targets_unchanged(self):
        rows = {"A": self.row(roe=0.4), "B": self.row(roe=0.2)}
        frame = self.frame(rows)
        default = self.generate(self.strategy(), frame)
        explicit = self.generate(self.strategy(trend_window=0), frame)
        self.assertEqual(default, explicit)
        self.assertEqual(self.strategy().requirements(), self.strategy(trend_window=0).requirements())
        self.assertNotIn("signal_close", self.strategy().requirements().fields)

    def test_trend_requests_adjusted_signal_and_required_history(self):
        request = self.strategy(trend_window=20).requirements()
        self.assertIn("signal_close", request.fields)
        self.assertEqual(request.history_bars, 20)

    def test_longer_trend_keeps_liquidity_window_at_twenty_sessions(self):
        count = 40
        frame = self.frame(
            {"A": self.row(signal_close=40.0)},
            history_counts={"A": count},
            history_signals={"A": list(range(1, count + 1))},
            history_amounts={"A": [0.0] * 20 + [1000.0] * 20},
            history_volumes={"A": [0.0] * 20 + [100.0] * 20},
        )
        request = self.strategy(trend_window=40).requirements()
        self.assertEqual(request.history_bars, 40)
        target = self.generate(self.strategy(trend_window=40), frame)
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_inclusive_twenty_day_sma_uses_signal_close_not_raw_close(self):
        frame = self.frame(
            {"A": self.row(close=1.0, signal_close=12.0),
             "B": self.row(close=1000.0, signal_close=8.0)},
            history_signals={"A": [10.0] * 19 + [12.0], "B": [10.0] * 19 + [8.0]},
        )
        target = self.generate(self.strategy(positions=20, trend_window=20), frame)
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_trend_equal_to_or_below_inclusive_sma_is_excluded(self):
        rows = {"EQUAL": self.row(signal_close=10.0), "BELOW": self.row(signal_close=9.0)}
        frame = self.frame(rows, history_signals={"BELOW": [10.0] * 19 + [9.0]})
        target = self.generate(self.strategy(trend_window=20), frame)
        self.assertEqual(dict(target.weights), {})

    def test_trend_missing_or_short_history_excludes_without_filling(self):
        rows = {"NONE": self.row(signal_close=None), "SHORT": self.row(signal_close=11.0)}
        frame = self.frame(rows, history_counts={"SHORT": 19})
        target = self.generate(self.strategy(trend_window=20), frame)
        self.assertEqual(dict(target.weights), {})

    def test_trend_missing_history_value_excludes_security(self):
        frame = self.frame(
            {"A": self.row(signal_close=12.0)},
            history_signals={"A": [10.0] * 19 + [None]},
        )
        self.assertEqual(dict(self.generate(self.strategy(trend_window=20), frame).weights), {})

    def test_trend_rejects_malformed_or_nonpositive_signal_prices(self):
        for bad in (True, "bad", float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=bad):
                frame = self.frame({"A": self.row(signal_close=bad)})
                with self.assertRaises(ValueError):
                    self.generate(self.strategy(trend_window=20), frame)

    def test_trend_rejects_malformed_history_signal_prices(self):
        for bad in (True, "bad", float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=bad):
                frame = self.frame(
                    {"A": self.row(signal_close=12.0)},
                    history_signals={"A": [10.0] * 19 + [bad]},
                )
                with self.assertRaises(ValueError):
                    self.generate(self.strategy(trend_window=20), frame)

    def test_trend_does_not_mutate_rows_or_history(self):
        frame = self.frame({"A": self.row(signal_close=11.0)})
        rows = {symbol: dict(row) for symbol, row in frame.fields.items()}
        history = {key: tuple((bar.at, dict(bar.fields)) for bar in value)
                   for key, value in frame.history.items()}
        self.generate(self.strategy(trend_window=20), frame)
        self.assertEqual({symbol: dict(row) for symbol, row in frame.fields.items()}, rows)
        self.assertEqual({key: tuple((bar.at, dict(bar.fields)) for bar in value)
                          for key, value in frame.history.items()}, history)

    def test_trend_parameter_rejects_bool_and_negative(self):
        for value in (True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.strategy(trend_window=value)


if __name__ == "__main__":
    unittest.main()
