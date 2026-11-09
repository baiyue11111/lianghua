"""Behavior tests for the point-in-time cash quality strategy."""
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch

from core.calculation import Calculation
from protocol import AccountSnapshot, HistoryBar, MarketFrame
from strategy import load_strategy


MODULE = "plugins.strategies.cash_quality"


class CashQualityTests(unittest.TestCase):
    def strategy(self, **params):
        return load_strategy(MODULE, params)

    def frame(self, rows, *, day="2026-06-30", following="2026-07-01",
              trading=True, history_counts=None, history_values=None):
        at = datetime.fromisoformat(day + "T15:00:00").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        history_counts = history_counts or {}
        history_values = history_values or {}
        history = {
            symbol: self.history(
                at,
                count=history_counts.get(symbol, 20),
                amount=history_values.get(symbol, (1000.0, 100.0))[0],
                volume=history_values.get(symbol, (1000.0, 100.0))[1],
            )
            for symbol in rows
        }
        return MarketFrame(at, "CN_EQUITY", rows,
                           {"trading": trading, "next_session": following}, history)

    def history(self, at, *, count=20, amount=1000.0, volume=100.0):
        days = []
        day = at.date()
        while len(days) < count:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.reverse()
        return tuple(
            HistoryBar(datetime.combine(day, time(15), tzinfo=at.tzinfo),
                       {"amount": amount, "volume": volume})
            for day in days
        )

    def row(self, **values):
        return {"roe": 0.1, "net_margin": 0.1, "debt_ratio": 0.3,
                "eps": 1.0, "cash_per_share": 1.0} | values

    def generate(self, strategy, frame):
        return strategy.generate(
            frame, AccountSnapshot(frame.at, Decimal("1000"), Decimal("1000"), {})
        )

    def test_requirements_use_five_facts_and_twenty_liquidity_sessions(self):
        request = self.strategy().requirements()
        self.assertEqual(
            request.fields,
            ("roe", "net_margin", "debt_ratio", "eps", "cash_per_share", "amount", "volume"),
        )
        self.assertEqual(request.history_bars, 20)

    def test_factor_directions_are_high_roe_high_margin_and_low_debt(self):
        strategy = self.strategy(positions=1)
        cases = (
            ({"roe": 0.2}, {"roe": 0.1}),
            ({"net_margin": 0.2}, {"net_margin": 0.1}),
            ({"debt_ratio": 0.1}, {"debt_ratio": 0.2}),
        )
        for preferred, other in cases:
            with self.subTest(preferred=preferred):
                target = self.generate(strategy, self.frame({
                    "A": self.row(**preferred),
                    "B": self.row(**other),
                }))
                self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_strategy_uses_equal_percentile_rank_factors(self):
        rows = {
            "A": self.row(roe=0.4, net_margin=0.1, debt_ratio=0.9),
            "B": self.row(roe=0.1, net_margin=0.4, debt_ratio=0.1),
            "C": self.row(roe=0.2, net_margin=0.2, debt_ratio=0.5),
        }
        with patch.object(Calculation, "fundamentals", wraps=Calculation.fundamentals) as call:
            target = self.generate(self.strategy(positions=1), self.frame(rows))
        self.assertEqual(dict(target.weights), {"B": 1.0})
        self.assertEqual(call.call_args.kwargs["ranking"], "rank")
        self.assertEqual(call.call_args.args[1],
                         {"roe": 1.0, "net_margin": 1.0, "debt_ratio": -1.0})

    def test_eps_and_cash_per_share_are_sign_checks_not_amount_scores(self):
        rows = {
            "A": self.row(eps=0.01, cash_per_share=0.01),
            "B": self.row(eps=1e12, cash_per_share=1e12),
        }
        target = self.generate(self.strategy(positions=1), self.frame(rows))
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_nonpositive_eps_or_cash_per_share_excludes_security(self):
        rows = {
            "A": self.row(eps=0),
            "B": self.row(eps=-1),
            "C": self.row(cash_per_share=0),
            "D": self.row(cash_per_share=-1),
            "E": self.row(),
        }
        target = self.generate(self.strategy(), self.frame(rows))
        self.assertEqual(dict(target.weights), {"E": 1.0})

    def test_roe_margin_and_debt_bounds_are_enforced(self):
        rows = {
            "GOOD": self.row(),
            "ZERO_ROE": self.row(roe=0),
            "ZERO_MARGIN": self.row(net_margin=0),
            "NEGATIVE_DEBT": self.row(debt_ratio=-0.01),
            "HIGH_DEBT": self.row(debt_ratio=1.01),
        }
        target = self.generate(self.strategy(), self.frame(rows))
        self.assertEqual(dict(target.weights), {"GOOD": 1.0})

    def test_none_facts_exclude_without_zero_fill_and_missing_keys_fail(self):
        rows = {"A": self.row(roe=None), "B": self.row()}
        self.assertEqual(dict(self.generate(self.strategy(), self.frame(rows)).weights),
                         {"B": 1.0})
        missing = self.row()
        del missing["cash_per_share"]
        with self.assertRaises(KeyError):
            self.generate(self.strategy(), self.frame({"A": missing}))

    def test_non_numeric_bool_and_nonfinite_facts_are_rejected(self):
        strategy = self.strategy()
        for field in ("roe", "net_margin", "debt_ratio", "eps", "cash_per_share"):
            for value in (True, "bad", float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.generate(strategy, self.frame({"A": self.row(**{field: value})}))

    def test_only_twenty_positive_amount_and_volume_sessions_qualify(self):
        rows = {symbol: self.row() for symbol in
                ("SHORT", "ZERO_AMOUNT", "NONE_AMOUNT", "ZERO_VOLUME", "NONE_VOLUME", "OLD")}
        frame = self.frame(
            rows,
            history_counts={"SHORT": 19},
            history_values={
                "ZERO_AMOUNT": (0.0, 100.0),
                "NONE_AMOUNT": (None, 100.0),
                "ZERO_VOLUME": (1000.0, 0.0),
                "NONE_VOLUME": (1000.0, None),
            },
        )
        target = self.generate(self.strategy(), frame)
        self.assertEqual(dict(target.weights), {"OLD": 1.0})

    def test_bad_liquidity_values_are_rejected(self):
        for value in (True, "bad", float("nan"), float("inf")):
            with self.subTest(value=value):
                frame = self.frame({"A": self.row()}, history_values={"A": (value, 100.0)})
                with self.assertRaises(ValueError):
                    self.generate(self.strategy(), frame)

    def test_month_inside_period_does_not_trade_and_month_end_reissues_target(self):
        strategy = self.strategy()
        rows = {"A": self.row()}
        inside = self.frame(rows, day="2026-07-02", following="2026-07-03")
        self.assertIsNone(self.generate(strategy, inside))
        june = self.generate(strategy, self.frame(rows))
        july = self.generate(strategy, self.frame(
            rows, day="2026-07-31", following="2026-08-03"))
        self.assertEqual(dict(june.weights), {"A": 1.0})
        self.assertEqual(dict(july.weights), {"A": 1.0})

    def test_nontrading_phase_emits_no_target_and_empty_pool_clears(self):
        strategy = self.strategy()
        rows = {"A": self.row()}
        self.assertIsNone(self.generate(strategy, self.frame(rows, trading=False)))
        empty = self.generate(strategy, self.frame({"A": self.row(eps=0)}))
        self.assertEqual(dict(empty.weights), {})

    def test_positions_cap_equal_weights_and_stable_tie_order(self):
        rows = {code: self.row() for code in ("C", "A", "B")}
        target = self.generate(self.strategy(positions=2), self.frame(rows))
        self.assertEqual(dict(target.weights), {"A": 0.5, "B": 0.5})
        self.assertAlmostEqual(sum(target.weights.values()), 1.0)

    def test_strategy_does_not_mutate_input_rows_or_context(self):
        rows = {"A": self.row(), "B": self.row(roe=0.2)}
        before_rows = deepcopy(rows)
        frame = self.frame(rows)
        before_context = dict(frame.context)
        self.generate(self.strategy(), frame)
        self.assertEqual(rows, before_rows)
        self.assertEqual(dict(frame.context), before_context)
