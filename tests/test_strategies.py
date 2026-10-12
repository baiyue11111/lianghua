from datetime import date
import unittest

from contracts import Candidate, StrategyFrame
from features import conservative_available_date
from strategies.quality_trend import QualityTrendStrategy
from strategies.trend_pullback import TrendPullbackStrategy
from strategies.era_leadership import EraLeadershipStrategy
from tests.legacy_breadth import BreadthLowAmountReversalStrategy


class StrategyTests(unittest.TestCase):
    def test_q4_becomes_available_on_following_may_first(self) -> None:
        self.assertEqual(conservative_available_date("2024-Q4"), date(2025, 5, 1))
        self.assertEqual(conservative_available_date("2024-Q2"), date(2024, 9, 1))

    def test_quality_trend_returns_ranked_equal_weight_targets_without_io(self) -> None:
        frame = StrategyFrame(
            as_of=date(2024, 6, 28),
            breadth_exposure=1.0,
            candidates=(
                Candidate("000001", roe=12, pe=10, pb=1, amount20=100_000_000, close=12, ma120=10, r120=0.2, net_income_ttm=1, net_assets=1),
                Candidate("000002", roe=20, pe=12, pb=1, amount20=100_000_000, close=14, ma120=10, r120=0.4, net_income_ttm=1, net_assets=1),
                Candidate("000003", roe=8, pe=12, pb=1, amount20=100_000_000, close=14, ma120=10, r120=0.9, net_income_ttm=1, net_assets=1),
            ),
        )

        target = QualityTrendStrategy({"positions": 3}).target(frame)

        self.assertEqual(target.strategy_id, "quality_trend")
        self.assertEqual(target.target_weights, {"000002": 0.5, "000001": 0.5})
        self.assertEqual(target.gross_exposure, 1.0)

    def test_pullback_selects_only_a_qualified_drawdown(self) -> None:
        frame = StrategyFrame(
            as_of=date(2024, 6, 28),
            breadth_exposure=1.0,
            candidates=(
                Candidate("000001", roe=12, pe=10, pb=1, amount20=100_000_000, close=10, ma120=9, r120=0.4, drawdown20=-0.10, ma20=11, net_income_ttm=1, net_assets=1),
                Candidate("000002", roe=12, pe=10, pb=1, amount20=100_000_000, close=10, ma120=9, r120=0.6, drawdown20=-0.03, ma20=11, net_income_ttm=1, net_assets=1),
            ),
        )

        target = TrendPullbackStrategy({"positions": 3, "drawdown": 0.08}).target(frame)

        self.assertEqual(target.target_weights, {"000001": 1.0})

    def test_era_uses_one_third_exposure_when_market_is_closed(self) -> None:
        frame = StrategyFrame(
            as_of=date(2024, 6, 28),
            breadth_exposure=0.0,
            active_industries=frozenset({"通信"}),
            candidates=(
                Candidate("300308", roe=15, pe=30, pb=5, amount20=500_000_000, amount120=300_000_000, close=20, ma120=15, r120=0.8, industry="通信", net_income_ttm=1, net_assets=1),
            ),
        )

        target = EraLeadershipStrategy({"positions": 3}).target(frame)

        self.assertEqual(target.target_weights, {"300308": 1 / 3})
        self.assertEqual(target.gross_exposure, 1 / 3)

    def test_breadth_reversal_uses_low_amount_then_20d_loss_order(self) -> None:
        frame = StrategyFrame(
            as_of=date(2024, 6, 28),
            breadth_exposure=0.0,
            market_breadth_120=0.64,
            market_breadth_120_smoothed=0.62,
            market_on=True,
            is_month_end=True,
            candidates=tuple(
                Candidate(
                    symbol=f"{index:06d}",
                    roe=None,
                    pe=None,
                    pb=None,
                    amount20=amount,
                    close=12,
                    ma120=10,
                    r120=-float(index),
                    r20=-float(index),
                )
                for index, amount in enumerate(range(80), start=1)
            ),
        )
        strategy = BreadthLowAmountReversalStrategy()
        # The synthetic amounts are below the real floor; replace them with
        # ten eligible values to isolate the low-amount/reversal ordering.
        eligible = tuple(
            Candidate(
                symbol=candidate.symbol,
                roe=None,
                pe=None,
                pb=None,
                amount20=50_000_000 + index * 1_000_000,
                close=candidate.close,
                ma120=candidate.ma120,
                r120=candidate.r120,
                r20=candidate.r20,
            )
            for index, candidate in enumerate(frame.candidates)
        )
        frame = StrategyFrame(
            as_of=frame.as_of,
            breadth_exposure=frame.breadth_exposure,
            candidates=eligible,
            market_breadth_120=frame.market_breadth_120,
            market_breadth_120_smoothed=frame.market_breadth_120_smoothed,
            market_on=True,
            is_month_end=True,
        )
        target = strategy.target(frame)
        self.assertEqual(len(target.target_weights), 8)
        self.assertEqual(list(target.target_weights), [f"{index:06d}" for index in range(8, 0, -1)])

    def test_breadth_reversal_clears_when_market_is_off(self) -> None:
        frame = StrategyFrame(
            as_of=date(2024, 6, 28),
            breadth_exposure=0.0,
            candidates=(),
            held_days={"000001": 5},
            market_on=False,
        )
        target = BreadthLowAmountReversalStrategy().target(frame)
        self.assertEqual(target.target_weights, {})
        self.assertEqual(target.reasons["__market__"], "market_breadth_below_45pct")

    def test_monthly_hold_is_not_a_daily_rebalance(self) -> None:
        frame = StrategyFrame(as_of=date(2024, 6, 20), breadth_exposure=1.0,
                              candidates=(), held_days={"000001": 5},
                              market_on=True, is_month_end=False)
        target = BreadthLowAmountReversalStrategy().target(frame)
        self.assertFalse(target.rebalance)

    def test_market_exit_is_an_executable_rebalance(self) -> None:
        frame = StrategyFrame(as_of=date(2024, 6, 20), breadth_exposure=0,
                              candidates=(), held_days={"000001": 5},
                              market_on=False, is_month_end=False)
        self.assertTrue(BreadthLowAmountReversalStrategy().target(frame).rebalance)

    def test_entry_cadence_changes_only_flat_midmonth_entry(self) -> None:
        from dataclasses import replace
        frame = StrategyFrame(
            as_of=date(2024, 6, 20), breadth_exposure=1,
            candidates=(Candidate("000001", None, None, None, 60_000_000,
                                  12, 10, None, r20=-0.12),),
            market_on=True, market_breadth_120_smoothed=0.60,
            is_month_end=False)
        monthly = BreadthLowAmountReversalStrategy({"entry_cadence": "month_end"})
        immediate = BreadthLowAmountReversalStrategy({"entry_cadence": "on_qualification"})
        self.assertEqual(monthly.target(frame).target_weights, {})
        self.assertEqual(immediate.target(frame).target_weights, {"000001": 0.125})
        self.assertTrue(immediate.target(frame).rebalance)
        held = replace(frame, held_days={"000001": 1})
        self.assertFalse(immediate.target(held).rebalance)
        self.assertEqual(immediate.target(replace(frame, is_month_end=True)),
                         monthly.target(replace(frame, is_month_end=True)))

    def test_hysteresis_does_not_allow_new_flat_entry_below_sixty_percent(self) -> None:
        from dataclasses import replace
        frame = StrategyFrame(
            as_of=date(2024, 6, 28), breadth_exposure=1,
            candidates=(Candidate("000001", None, None, None, 60_000_000,
                                  12, 10, None, r20=-0.12),),
            market_on=True, market_breadth_120_smoothed=0.59,
            is_month_end=True)
        for cadence in ("month_end", "on_qualification"):
            strategy = BreadthLowAmountReversalStrategy({"entry_cadence": cadence})
            self.assertEqual(strategy.target(frame).target_weights, {})
            self.assertEqual(strategy.target(replace(frame, held_days={"000002": 10})).target_weights,
                             {"000001": 0.125})

    def test_flat_entry_qualification_failure_replaces_pending_target_on_any_day(self):
        from dataclasses import replace
        frame=StrategyFrame(as_of=date(2024,6,20),breadth_exposure=1,
                            candidates=(),held_days={},market_on=True,
                            market_breadth_120_smoothed=0.59,is_month_end=False)
        for cadence in ('month_end','on_qualification'):
            strategy=BreadthLowAmountReversalStrategy({'entry_cadence':cadence})
            for month_end in (False,True):
                target=strategy.target(replace(frame,is_month_end=month_end))
                self.assertEqual(target.target_weights,{})
                self.assertTrue(target.rebalance)
                self.assertEqual(target.reasons['__market__'],'wait_for_60pct_entry')
            held=strategy.target(replace(frame,held_days={'000001':2}))
            self.assertFalse(held.rebalance)
            self.assertEqual(held.target_weights,{'000001':0.125})


if __name__ == "__main__":
    unittest.main()
