"""Synthetic price jumps isolate order lifecycle, not investment performance."""
from datetime import date
import unittest

import numpy as np

from contracts import Side, TargetPortfolio
from research import ExploratoryMarket
from tests.legacy_research import run_strategy, _market_breadth_state


class FixedTarget:
    strategy_id = 'order-lifecycle-fixture'
    signal_frequency = 'daily'
    use_market_gate = False

    def __init__(self, exit_date=None):
        self.exit_date = exit_date

    def target(self, frame):
        exit_now = self.exit_date is not None and frame.as_of >= self.exit_date
        weights = {} if exit_now else {'000001': 0.4}
        return TargetPortfolio(self.strategy_id, frame.as_of, weights,
                               0 if exit_now else 0.4, {'000001': 'fixture'})


class HoldTarget(FixedTarget):
    def target(self, frame):
        target = super().target(frame)
        from dataclasses import replace
        return replace(target, rebalance=frame.as_of == self.first_day)


class ResearchOrderLifecycleTests(unittest.TestCase):
    def test_hold_does_not_cancel_or_resize_partial_order(self):
        market = self.market()
        strategy = HoldTarget()
        strategy.first_day = market.calendar[120]
        result = run_strategy(market, strategy, 10000)
        self.assertEqual(len(result.orders), 1)
        self.assertEqual([fill.shares for fill in result.fills], [200, 200])
        self.assertEqual({fill.order_id for fill in result.fills}, {result.orders[0].order_id})

    def test_breadth_does_not_treat_short_history_as_ma120(self):
        market = self.market()
        market.close[0, :10] = np.nan
        market.close[0, 123] = 20
        raw, _, _, _ = _market_breadth_state(market)
        self.assertEqual(raw[123], 0.0)

    def test_breadth_requires_full_twenty_amount_observations(self):
        market = self.market()
        market.amount[0, 104:123] = np.nan
        raw, _, _, _ = _market_breadth_state(market)
        # The live final bar still has twenty earlier own observations.
        self.assertTrue(np.isfinite(raw[123]))
        # A missing current amount bar must not reuse yesterday's liquidity.
        self.assertTrue(np.isnan(raw[122]))

    def test_breadth_skips_nontrading_placeholder_with_carry_forward_close(self):
        market = self.market()
        for values in (market.open, market.high, market.low, market.volume, market.amount):
            values[0, 120] = 0.0
        # A provider may carry yesterday's close through a suspended session.
        raw, _, _, _ = _market_breadth_state(market)
        self.assertTrue(np.isnan(raw[120]))
        self.assertEqual(raw[121], 1.0)

    def test_repeated_full_loop_has_identical_orders_fills_and_equity(self):
        market = self.market()
        day = market.calendar[120]
        first = run_strategy(market, FixedTarget(), 10000, day, day)
        second = run_strategy(market, FixedTarget(), 10000, day, day)
        self.assertEqual(first, second)
        self.assertGreater(len(first.fills), 0)

    def market(self):
        dates = tuple(date.fromisoformat(str(day)) for day in
                      np.busday_offset(np.datetime64('2020-01-06'), np.arange(124)))
        prices = np.full((1, 124), 10.0)
        prices[0, 121] = 40.0
        prices[0, 122:] = 5.0
        close = prices.copy()
        high = prices + 1
        return ExploratoryMarket(
            calendar=dates, symbols=('000001',), names=('fixture',),
            open=prices.copy(), high=high, low=prices - 1,
            close=close, signal_close=close, signal_high=high,
            volume=np.full_like(prices, 1e7),
            amount=np.full_like(prices, 1e8), industry=(None,),
            fundamental_periods=(), fundamentals={key: np.empty((1, 0))
                for key in ('roe', 'pe', 'pb', 'net_income_ttm', 'net_assets')})

    def test_remainder_executes_next_day_with_original_order_identity(self):
        market = self.market()
        signal_day = market.calendar[120]
        result = run_strategy(market, FixedTarget(), 10000, signal_day, signal_day)
        self.assertEqual(len(result.orders), 1)
        self.assertEqual(result.orders[0].requested_shares, 400)
        self.assertEqual([fill.shares for fill in result.fills], [200, 200])
        self.assertEqual([fill.trade_date for fill in result.fills], list(market.calendar[121:123]))
        self.assertEqual({fill.order_id for fill in result.fills}, {result.orders[0].order_id})

    def test_new_exit_target_cancels_outstanding_purchase(self):
        market = self.market()
        exit_day = market.calendar[121]
        result = run_strategy(market, FixedTarget(exit_day), 10000,
                              market.calendar[120], exit_day)
        self.assertEqual([fill.side for fill in result.fills], [Side.BUY, Side.SELL])
        self.assertEqual([fill.shares for fill in result.fills], [200, 200])
        self.assertNotEqual(result.fills[0].order_id, result.fills[1].order_id)
        self.assertEqual(result.holdings[-1]['shares'], '{}')
