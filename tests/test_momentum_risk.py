"""Behavior checks for the point-in-time momentum risk-budget plugin."""
from datetime import datetime, timedelta
from decimal import Decimal
from math import isclose
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.calculation import Calculation
from protocol import AccountSnapshot, HistoryBar, MarketFrame
from strategy import load_strategy


MODULE = "plugins.strategies.momentum_risk"


class MomentumRiskTests(unittest.TestCase):
    def account(self, frame, positions=None):
        return AccountSnapshot(frame.at, Decimal("1000000"), Decimal("1000000"), positions or {})

    def frame(self, values, *, start="2024-01-01", next_session=None):
        first = datetime.fromisoformat(start).replace(hour=15, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = {}
        count = max(len(items) for items in values.values())
        dates = [first + timedelta(days=index) for index in range(count)]
        for symbol, prices in values.items():
            history[symbol] = tuple(
                HistoryBar(dates[index], {"signal_close": price})
                for index, price in enumerate(prices) if price is not None
            )
        at = dates[-1]
        next_day = next_session or (at.date() + timedelta(days=1)).isoformat()
        return MarketFrame(at, "CN_EQUITY", {}, {"trading": True, "next_session": next_day}, history)

    def strategy(self, **params):
        return load_strategy(MODULE, params)

    def test_request_covers_skip_momentum_and_daily_volatility_history(self):
        request = self.strategy().requirements()
        self.assertEqual(request.fields, ("signal_close",))
        self.assertEqual(request.history_bars, 121)

    def test_month_end_ranks_return_from_day_120_to_day_20(self):
        strategy = self.strategy(
            positions=1, momentum_window=4, skip_window=1,
            volatility_window=3, allocation="equal",
        )
        frame = self.frame({
            "A": [100, 110, 120, 130, 20],
            "B": [100, 80, 90, 100, 1000],
        }, start="2024-01-27", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_non_month_end_does_not_emit_a_target(self):
        strategy = self.strategy(
            positions=1, momentum_window=4, skip_window=1,
            volatility_window=3, allocation="equal",
        )
        frame = self.frame({"A": [100, 110, 120, 130, 140]},
                           start="2024-01-26", next_session="2024-01-31")
        self.assertIsNone(strategy.generate(frame, self.account(frame)))

    def test_weekly_does_not_emit_inside_the_same_iso_week(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal",
                                 rebalance_period="weekly")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2024-01-01",
                           next_session="2024-01-05")
        self.assertIsNone(strategy.generate(frame, self.account(frame)))

    def test_weekly_emits_when_next_session_is_in_a_new_iso_week(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal",
                                 rebalance_period="weekly")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2024-01-02",
                           next_session="2024-01-08")
        self.assertIsNotNone(strategy.generate(frame, self.account(frame)))

    def test_weekly_does_not_emit_across_calendar_year_with_same_iso_week(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal",
                                 rebalance_period="weekly")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2020-12-28",
                           next_session="2021-01-01")
        self.assertIsNone(strategy.generate(frame, self.account(frame)))

    def test_weekly_emits_when_next_session_starts_a_new_iso_year(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal",
                                 rebalance_period="weekly")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2020-12-29",
                           next_session="2021-01-04")
        self.assertIsNotNone(strategy.generate(frame, self.account(frame)))

    def test_weekly_month_end_overlap_emits_one_equivalent_target(self):
        common = dict(positions=1, momentum_window=3, skip_window=1,
                      volatility_window=3, allocation="equal")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2024-03-26",
                           next_session="2024-04-01")
        monthly = self.strategy(**common)
        weekly = self.strategy(**common, rebalance_period="weekly")
        month_target = monthly.generate(frame, self.account(frame))
        week_target = weekly.generate(frame, self.account(frame))
        self.assertIsNotNone(month_target)
        self.assertIsNotNone(week_target)
        self.assertEqual(dict(week_target.weights), dict(month_target.weights))
        self.assertEqual(dict(week_target.reasons), dict(month_target.reasons))

    def test_unknown_rebalance_period_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "rebalance_period"):
            self.strategy(rebalance_period="daily")

    def test_rebalance_period_does_not_change_history_request(self):
        request = self.strategy(momentum_window=20, skip_window=0,
                                volatility_window=60, trend_window=60,
                                rebalance_period="weekly").requirements()
        self.assertEqual(request.fields, ("signal_close",))
        self.assertEqual(request.history_bars, 61)

    def test_default_monthly_matches_explicit_monthly_targets(self):
        common = dict(positions=1, momentum_window=3, skip_window=1,
                      volatility_window=3, allocation="equal")
        frame = self.frame({"A": [100, 101, 102, 103]}, start="2024-01-28",
                           next_session="2024-02-01")
        default = self.strategy(**common)
        explicit = self.strategy(**common, rebalance_period="monthly")
        expected = default.generate(frame, self.account(frame))
        actual = explicit.generate(frame, self.account(frame))
        self.assertEqual(dict(actual.weights), dict(expected.weights))
        self.assertEqual(dict(actual.reasons), dict(expected.reasons))

    def test_inverse_volatility_gives_half_volatility_name_twice_weight(self):
        strategy = self.strategy(
            positions=2, momentum_window=3, skip_window=1,
            volatility_window=3, allocation="inverse_vol",
        )
        frame = self.frame({
            "A": [100, 101, 103.02, 104.0502],
            "B": [100, 102, 106.08, 108.2016],
        }, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertAlmostEqual(target.weights["A"] / target.weights["B"], 2.0, places=6)
        self.assertAlmostEqual(sum(target.weights.values()), 1.0, places=12)

    def test_risk_budget_reduces_gross_exposure_when_forecast_risk_is_high(self):
        strategy = self.strategy(
            positions=1, momentum_window=3, skip_window=1,
            volatility_window=3, target_volatility=0.1,
        )
        frame = self.frame({"A": [100, 120, 105, 130]},
                           start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        gross = sum(target.weights.values())
        self.assertGreater(gross, 0.0)
        self.assertLess(gross, 1.0)

    def test_equal_allocation_is_an_ablation_of_risk_weighting(self):
        strategy = self.strategy(
            positions=2, momentum_window=3, skip_window=1,
            volatility_window=3, allocation="equal",
        )
        frame = self.frame({
            "A": [100, 101, 103.02, 104.0502],
            "B": [100, 102, 106.08, 108.2016],
        }, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": 0.5, "B": 0.5})

    def test_missing_price_excludes_only_that_security(self):
        strategy = self.strategy(
            positions=2, momentum_window=3, skip_window=1,
            volatility_window=3, allocation="equal",
        )
        frame = self.frame({
            "A": [100, 101, 103.02, 104.0502],
            "B": [100, 102, None, 108.2016],
        }, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame, {"B": Decimal("2")}))
        self.assertEqual(dict(target.weights), {"A": 1.0})

    def test_zero_volatility_name_is_not_used_for_inverse_weights(self):
        strategy = self.strategy(
            positions=1, momentum_window=3, skip_window=1,
            volatility_window=2, allocation="inverse_vol",
        )
        frame = self.frame({"A": [100, 110, 110, 110]},
                           start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {})

    def test_month_end_recomputes_weights_even_when_selection_is_unchanged(self):
        strategy = self.strategy(
            positions=1, momentum_window=3, skip_window=1,
            volatility_window=3, target_volatility=0.1,
        )
        calm = self.frame({"A": [100, 101, 102, 103.0301]},
                          start="2024-01-28", next_session="2024-02-01")
        volatile = self.frame({"A": [100, 120, 105, 130]},
                              start="2024-02-25", next_session="2024-03-01")
        first = strategy.generate(calm, self.account(calm))
        second = strategy.generate(volatile, self.account(volatile))
        self.assertIsNotNone(second)
        self.assertEqual(set(first.weights), set(second.weights))
        self.assertNotEqual(first.weights["A"], second.weights["A"])

    def test_non_finite_history_is_rejected(self):
        strategy = self.strategy(
            positions=1, momentum_window=3, skip_window=1,
            volatility_window=3,
        )
        frame = self.frame({"A": [100, 101, float("nan"), 104]},
                           start="2024-01-28", next_session="2024-02-01")
        with self.assertRaisesRegex(ValueError, "finite positive"):
            strategy.generate(frame, self.account(frame))

    def test_inverse_weights_fit_decimal_accounting_sum(self):
        strategy = self.strategy(positions=5, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="inverse_vol")
        values = {}
        for i in range(1, 6):
            values[str(i)] = [100, 100 * (1 + .01 * i),
                              100 * (1 + .01 * i) * (1 + .02 * i),
                              100 * (1 + .01 * i) * (1 + .02 * i) * (1 + .01 * i)]
        frame = self.frame(values, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertLessEqual(sum(Decimal(str(weight)) for weight in target.weights.values()), Decimal("1"))

    def test_risk_budget_matches_hand_computed_weights(self):
        strategy = self.strategy(positions=2, momentum_window=3, skip_window=1,
                                 volatility_window=3, target_volatility=.06)
        frame = self.frame({
            "A": [100, 101, 103.02, 104.0502],
            "B": [100, 102, 106.08, 108.2016],
        }, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertTrue(isclose(target.weights["A"], .32732683535, rel_tol=1e-8))
        self.assertTrue(isclose(target.weights["B"], .16366341768, rel_tol=1e-8))

    def test_zero_portfolio_risk_uses_full_gross(self):
        strategy = self.strategy(positions=2, momentum_window=4, skip_window=3,
                                 volatility_window=2, target_volatility=.1)
        frame = self.frame({
            "A": [100, 110, 100, 120, 96],
            "B": [100, 110, 100, 80, 96],
        }, start="2024-01-27", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"A", "B"})
        self.assertEqual(sum(Decimal(str(weight)) for weight in target.weights.values()), Decimal("1"))

    def test_nonpositive_momentum_is_excluded(self):
        strategy = self.strategy(positions=2, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal")
        frame = self.frame({
            "A": [100, 99, 98, 97],
            "B": [100, 101, 102, 103],
        }, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"B"})
        self.assertEqual(target.weights["B"], 1.0)

    def test_missing_field_is_rejected_but_none_is_unready(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3)
        frame = self.frame({"A": [100, 101, 102, 103]},
                           start="2024-01-28", next_session="2024-02-01")
        row = frame.history["A"][-1]
        history = dict(frame.history)
        history["A"] = (*frame.history["A"][:-1], HistoryBar(row.at, {}))
        missing = MarketFrame(frame.at, frame.venue, frame.fields, frame.context, history)
        with self.assertRaises(KeyError):
            strategy.generate(missing, self.account(missing))

        values = {"A": [100, 101, 102, None], "B": [100, 101, 102, 103]}
        none_frame = self.frame(values, start="2024-01-28", next_session="2024-02-01")
        target = strategy.generate(none_frame, self.account(none_frame))
        self.assertEqual(set(target.weights), {"B"})

    def test_risk_adjusted_ranking_changes_only_the_selected_name(self):
        values = {
            "A": [100, 130, 140, 150],
            "B": [100, 110, 130, 140],
        }
        raw = self.strategy(positions=1, momentum_window=3, skip_window=1,
                            volatility_window=3, allocation="equal")
        adjusted = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                 volatility_window=3, allocation="equal",
                                 ranking="risk_adjusted")
        frame = self.frame(values, start="2024-01-28", next_session="2024-02-01")
        self.assertEqual(set(raw.generate(frame, self.account(frame)).weights), {"A"})
        self.assertEqual(set(adjusted.generate(frame, self.account(frame)).weights), {"B"})

    def test_default_ranking_remains_raw_momentum(self):
        strategy = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                volatility_window=3, allocation="equal")
        frame = self.frame({"A": [100, 130, 140, 150], "B": [100, 110, 130, 140]},
                           start="2024-01-28", next_session="2024-02-01")
        self.assertEqual(set(strategy.generate(frame, self.account(frame)).weights), {"A"})

    def test_unknown_ranking_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ranking"):
            self.strategy(ranking="future_return")

    def test_reversal_and_momentum_select_opposite_return_directions(self):
        values = {"A": [100, 105, 110, 115, 120],
                  "B": [100, 90, 80, 70, 60]}
        momentum = self.strategy(positions=1, momentum_window=4, skip_window=0,
                                 volatility_window=4, allocation="equal", ranking="momentum")
        reversal = self.strategy(positions=1, momentum_window=4, skip_window=0,
                                 volatility_window=4, allocation="equal", ranking="reversal")
        frame = self.frame(values, start="2024-01-27", next_session="2024-02-01")
        self.assertEqual(set(momentum.generate(frame, self.account(frame)).weights), {"A"})
        target = reversal.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"B"})
        self.assertIn("reversal", next(iter(target.reasons.values())))

    def test_reversal_ranks_by_raw_loss(self):
        strategy = self.strategy(positions=1, momentum_window=4, skip_window=0,
                                 volatility_window=4, allocation="equal", ranking="reversal")
        frame = self.frame({
            "A": [100, 160, 100, 130, 60],
            "B": [100, 95, 90, 85, 80],
        }, start="2024-01-27", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"A"})

    def test_reversal_has_no_target_when_all_momentum_is_positive(self):
        strategy = self.strategy(positions=2, momentum_window=4, skip_window=0,
                                 volatility_window=4, allocation="equal", ranking="reversal")
        frame = self.frame({
            "A": [100, 101, 102, 103, 104],
            "B": [100, 102, 103, 105, 106],
        }, start="2024-01-27", next_session="2024-02-01")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {})

    def test_trend_filter_keeps_long_rising_short_term_loser(self):
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 allocation="equal", ranking="reversal")
        rising = [100.0] * 61
        rising[40], rising[60] = 150.0, 130.0
        falling = [150.0] * 61
        falling[40], falling[60] = 100.0, 50.0
        frame = self.frame({"A": rising, "B": falling}, start="2024-01-01",
                           next_session="2024-04-01")
        closed = self.strategy(positions=1, momentum_window=20, skip_window=0,
                               volatility_window=60, trend_window=0,
                               allocation="equal", ranking="reversal")
        self.assertEqual(set(closed.generate(frame, self.account(frame)).weights), {"B"})
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"A"})

    def test_trend_filter_rejects_flat_long_return(self):
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 allocation="equal", ranking="reversal")
        flat = [100.0] * 61
        flat[40] = 120.0
        frame = self.frame({"A": flat}, start="2024-01-01", next_session="2024-04-01")
        self.assertEqual(dict(strategy.generate(frame, self.account(frame)).weights), {})

    def test_trend_filter_is_applied_to_momentum_ranking_too(self):
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 allocation="equal", ranking="momentum")
        falling = [150.0] * 61
        falling[40], falling[60] = 100.0, 110.0
        frame = self.frame({"A": falling}, start="2024-01-01", next_session="2024-04-01")
        self.assertEqual(dict(strategy.generate(frame, self.account(frame)).weights), {})

    def test_missing_trend_price_inside_window_excludes_that_security(self):
        strategy = self.strategy(positions=2, momentum_window=20, skip_window=0,
                                 volatility_window=3, trend_window=60,
                                 allocation="equal", ranking="reversal")
        missing = [100.0] * 61
        missing[40], missing[60] = 150.0, 130.0
        missing[30] = None
        ready = [100.0] * 61
        ready[40], ready[60] = 150.0, 130.0
        frame = self.frame({"A": missing, "B": ready}, start="2024-01-01",
                           next_session="2024-04-01")
        closed = self.strategy(positions=2, momentum_window=20, skip_window=0,
                               volatility_window=3, trend_window=0,
                               allocation="equal", ranking="reversal")
        self.assertEqual(set(closed.generate(frame, self.account(frame)).weights), {"A", "B"})
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"B"})

    def test_trend_window_is_included_in_history_request(self):
        short = self.strategy(momentum_window=20, skip_window=0, volatility_window=60,
                              trend_window=60).requirements()
        closed = self.strategy(momentum_window=20, skip_window=0, volatility_window=60,
                               trend_window=0).requirements()
        long = self.strategy(momentum_window=20, skip_window=0, volatility_window=60,
                             trend_window=120).requirements()
        self.assertEqual(short.history_bars, 61)
        self.assertEqual(closed.history_bars, 61)
        self.assertEqual(long.history_bars, 121)

    def test_breadth_filter_does_not_change_history_request(self):
        request = self.strategy(momentum_window=20, skip_window=0, volatility_window=60,
                                trend_window=60, breadth_min=0.5).requirements()
        self.assertEqual(request.fields, ("signal_close",))
        self.assertEqual(request.history_bars, 61)

    def test_breadth_min_requires_a_trend_window(self):
        with self.assertRaisesRegex(ValueError, "trend_window"):
            self.strategy(trend_window=0, breadth_min=0.5)

    def test_breadth_filter_below_threshold_clears_targets(self):
        rising = [100.0] * 61
        rising[40], rising[60] = 150.0, 130.0
        falling = [150.0] * 61
        falling[40], falling[60] = 100.0, 50.0
        frame = self.frame({"A": rising, "B": falling, "C": falling, "D": falling},
                           start="2024-01-01", next_session="2024-04-01")
        strategy = self.strategy(positions=2, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal", ranking="reversal")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {})
        self.assertEqual(dict(target.reasons), {})

    def test_breadth_equal_to_threshold_preserves_targets(self):
        rising = [100.0] * 61
        rising[40], rising[60] = 150.0, 130.0
        rising_two = [100.0] * 61
        rising_two[40], rising_two[60] = 140.0, 120.0
        falling = [150.0] * 61
        falling[40], falling[60] = 100.0, 50.0
        frame = self.frame({"A": rising, "B": rising_two, "C": falling, "D": falling},
                           start="2024-01-01", next_session="2024-04-01")
        common = dict(positions=2, momentum_window=20, skip_window=0,
                      volatility_window=60, trend_window=60,
                      allocation="equal", ranking="reversal")
        control = self.strategy(**common)
        gated = self.strategy(**common, breadth_min=0.5)
        expected = control.generate(frame, self.account(frame))
        actual = gated.generate(frame, self.account(frame))
        self.assertEqual(dict(actual.weights), dict(expected.weights))
        self.assertEqual(dict(actual.reasons), dict(expected.reasons))

    def test_trend_uses_current_inclusive_mean_instead_of_old_endpoint(self):
        old_endpoint_only = [100.0] + [150.0] * 39 + [150.0] + [130.0] * 20
        mean_only = [200.0] + [100.0] * 39 + [130.0] + [110.0] * 20
        frame = self.frame({"A": old_endpoint_only, "B": mean_only},
                           start="2024-01-01", next_session="2024-04-01")
        strategy = self.strategy(positions=2, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 allocation="equal", ranking="reversal")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"B"})

    def test_trend_mean_comparison_is_strict_at_the_mean(self):
        equal_to_mean = [99.0] + [100.0] * 39 + [101.0] + [100.0] * 20
        frame = self.frame({"A": equal_to_mean}, start="2024-01-01",
                           next_session="2024-04-01")
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 allocation="equal", ranking="reversal")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {})

    def test_missing_trend_price_is_excluded_and_not_counted_in_breadth(self):
        missing = [200.0] + [100.0] * 29 + [None] + [100.0] * 9 + [120.0] + [100.0] * 20
        rising = [100.0] * 60 + [120.0]
        falling = [200.0] + [150.0] * 59 + [100.0]
        reversal = [100.0] * 40 + [130.0] + [120.0] * 20
        frame = self.frame({"A": missing, "B": rising, "C": falling, "D": reversal},
                           start="2024-01-01", next_session="2024-04-01")
        strategy = self.strategy(positions=2, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=2 / 3, allocation="equal", ranking="reversal")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"D"})

    def test_complete_trend_window_is_averaged_once_per_security(self):
        frame = self.frame({"A": [100.0] * 60 + [101.0],
                            "B": [100.0] * 60 + [102.0]},
                           start="2024-01-01", next_session="2024-04-01")
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal")
        original = Calculation.sma
        with patch.object(Calculation, "sma", wraps=original) as sma:
            strategy.generate(frame, self.account(frame))
        self.assertEqual(sma.call_count, 2)

    def test_missing_breadth_endpoints_are_not_filled_or_counted(self):
        rising = [100.0] * 61
        rising[40], rising[60] = 150.0, 130.0
        falling = [150.0] * 61
        falling[40], falling[60] = 100.0, 50.0
        missing = [100.0] * 61
        missing[0] = None
        missing[40], missing[60] = 150.0, 130.0
        frame = self.frame({"A": rising, "B": falling, "C": missing},
                           start="2024-01-01", next_session="2024-04-01")
        strategy = self.strategy(positions=2, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal", ranking="reversal")
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"A"})

    def test_breadth_filter_rejects_when_no_endpoints_are_measurable(self):
        missing_a = [100.0] * 61
        missing_b = [100.0] * 61
        missing_a[30] = None
        missing_b[31] = None
        frame = self.frame({"A": missing_a, "B": missing_b}, start="2024-01-01",
                           next_session="2024-04-01")
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal", ranking="reversal")
        with self.assertRaisesRegex(ValueError, "market breadth"):
            strategy.generate(frame, self.account(frame))

    def test_breadth_filter_rejects_empty_history(self):
        at = datetime.fromisoformat("2024-01-31").replace(
            hour=15, tzinfo=ZoneInfo("Asia/Shanghai"))
        frame = MarketFrame(at, "CN_EQUITY", {},
                            {"trading": True, "next_session": "2024-02-01"}, {})
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal", ranking="reversal")
        with self.assertRaisesRegex(ValueError, "market breadth"):
            strategy.generate(frame, self.account(frame))

    def test_breadth_filter_rejects_history_without_a_trend_endpoint(self):
        frame = self.frame({"A": [100.0] * 60}, start="2024-01-01",
                           next_session="2024-04-01")
        strategy = self.strategy(positions=1, momentum_window=20, skip_window=0,
                                 volatility_window=60, trend_window=60,
                                 breadth_min=0.5, allocation="equal", ranking="reversal")
        with self.assertRaisesRegex(ValueError, "market breadth"):
            strategy.generate(frame, self.account(frame))

    def test_disabled_breadth_filter_preserves_all_modes(self):
        frame = self.frame({"A": [100, 101, 102, 103],
                            "B": [100, 99, 97, 96]},
                           start="2024-01-28", next_session="2024-02-01")
        for ranking in ("momentum", "risk_adjusted", "reversal"):
            for allocation in ("equal", "inverse_vol", "risk_budget"):
                with self.subTest(ranking=ranking, allocation=allocation):
                    common = dict(positions=1, momentum_window=3, skip_window=1,
                                  volatility_window=3, trend_window=0,
                                  allocation=allocation, ranking=ranking)
                    old = self.strategy(**common)
                    disabled = self.strategy(**common, breadth_min=0)
                    expected = old.generate(frame, self.account(frame))
                    actual = disabled.generate(frame, self.account(frame))
                    self.assertEqual(dict(actual.weights), dict(expected.weights))
                    self.assertEqual(dict(actual.reasons), dict(expected.reasons))

    def test_negative_trend_window_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "trend_window"):
            self.strategy(trend_window=-1)

    def test_disabled_trend_window_preserves_all_ranking_and_allocation_modes(self):
        frame = self.frame({
            "A": [100, 101, 102, 103],
            "B": [100, 99, 97, 96],
        }, start="2024-01-28", next_session="2024-02-01")
        for ranking in ("momentum", "risk_adjusted", "reversal"):
            for allocation in ("equal", "inverse_vol", "risk_budget"):
                with self.subTest(ranking=ranking, allocation=allocation):
                    defaults = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                             volatility_window=3, allocation=allocation,
                                             ranking=ranking)
                    closed = self.strategy(positions=1, momentum_window=3, skip_window=1,
                                           volatility_window=3, allocation=allocation,
                                           ranking=ranking, trend_window=0)
                    expected = defaults.generate(frame, self.account(frame))
                    actual = closed.generate(frame, self.account(frame))
                    self.assertTrue(expected.weights)
                    self.assertEqual(dict(actual.weights), dict(expected.weights))
                    self.assertEqual(dict(actual.reasons), dict(expected.reasons))

    def test_trim_skips_floor_fraction_of_ranked_candidates(self):
        frame = self.frame({
            "A": [100, 80, 60],
            "B": [100, 90, 70],
            "C": [100, 95, 80],
            "D": [100, 95, 90],
            "E": [100, 98, 95],
        }, start="2024-01-01", next_session="2024-01-08")
        strategy = self.strategy(positions=2, momentum_window=2, skip_window=0,
                                 volatility_window=2, allocation="equal",
                                 ranking="reversal", rebalance_period="weekly",
                                 trim=0.20)
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"B": 0.5, "C": 0.5})

    def test_trim_must_be_in_the_half_open_unit_interval(self):
        invalid = ((-0.01, "below minimum"), (1.0, "less than 1"),
                   (1.01, "above maximum"), (float("nan"), "finite float"))
        for trim, message in invalid:
            with self.subTest(trim=trim), self.assertRaisesRegex(ValueError, message):
                self.strategy(trim=trim)

    def test_trim_zero_preserves_targets_for_all_rankings_and_allocations(self):
        frame = self.frame({"A": [100, 101, 102, 103],
                            "B": [100, 99, 97, 96]},
                           start="2024-01-01", next_session="2024-01-08")
        for ranking in ("momentum", "risk_adjusted", "reversal"):
            for allocation in ("equal", "inverse_vol", "risk_budget"):
                with self.subTest(ranking=ranking, allocation=allocation):
                    common = dict(positions=1, momentum_window=3, skip_window=1,
                                  volatility_window=3, allocation=allocation,
                                  ranking=ranking, rebalance_period="weekly")
                    old = self.strategy(**common)
                    zero_trim = self.strategy(**common, trim=0.0)
                    expected = old.generate(frame, self.account(frame))
                    actual = zero_trim.generate(frame, self.account(frame))
                    self.assertEqual(dict(actual.weights), dict(expected.weights))
                    self.assertEqual(dict(actual.reasons), dict(expected.reasons))

    def test_trim_applies_after_each_ranking_mode(self):
        cases = (
            ("momentum", {"A": [100, 110, 120], "B": [100, 120, 140],
                           "C": [100, 130, 160], "D": [100, 140, 180],
                           "E": [100, 150, 200]}, {"C", "D"}),
            ("risk_adjusted", {"A": [100, 110, 120], "B": [100, 120, 140],
                                "C": [100, 130, 160], "D": [100, 140, 180],
                                "E": [100, 150, 200]}, {"B", "C"}),
            ("reversal", {"A": [100, 80, 60], "B": [100, 90, 70],
                          "C": [100, 95, 80], "D": [100, 95, 90],
                          "E": [100, 98, 95]}, {"B", "C"}),
        )
        for ranking, values, expected in cases:
            with self.subTest(ranking=ranking):
                frame = self.frame(values, start="2024-01-01", next_session="2024-01-08")
                strategy = self.strategy(positions=2, momentum_window=2, skip_window=0,
                                         volatility_window=2, allocation="equal",
                                         ranking=ranking, rebalance_period="weekly",
                                         trim=0.20)
                target = strategy.generate(frame, self.account(frame))
                self.assertEqual(set(target.weights), expected)
                self.assertEqual(len(target.weights), 2)

    def test_trim_fraction_uses_only_candidates_with_complete_data(self):
        frame = self.frame({
            "A": [100, 80, 60],
            "B": [100, 90, 70],
            "C": [100, 95, 80],
            "D": [100, 95, 90],
            "MISSING": [100, None, 50],
        }, start="2024-01-01", next_session="2024-01-08")
        strategy = self.strategy(positions=2, momentum_window=2, skip_window=0,
                                 volatility_window=2, allocation="equal",
                                 ranking="reversal", rebalance_period="weekly",
                                 trim=0.20)
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": 0.5, "B": 0.5})

    def test_trim_uses_decimal_fraction_at_integer_boundary(self):
        values = {f"S{index:03d}": [100, 99 - index * 0.1, 80 - index * 0.5]
                  for index in range(100)}
        frame = self.frame(values, start="2024-01-01", next_session="2024-01-08")
        strategy = self.strategy(positions=1, momentum_window=2, skip_window=0,
                                 volatility_window=2, allocation="equal",
                                 ranking="reversal", rebalance_period="weekly",
                                 trim=0.29)
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(set(target.weights), {"S070"})


if __name__ == "__main__":
    unittest.main()
