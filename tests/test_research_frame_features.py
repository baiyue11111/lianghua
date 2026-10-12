"""Candidate features must use each symbol's observed bars as of the signal."""

from dataclasses import replace
from datetime import date
import unittest

import numpy as np

from research import ExploratoryMarket, _bar, _frame_at, _market_breadth_state


class ResearchFrameFeatureTests(unittest.TestCase):
    def market(self) -> ExploratoryMarket:
        days = 124
        dates = tuple(
            date.fromisoformat(str(day))
            for day in np.busday_offset(np.datetime64("2020-01-06"), np.arange(days))
        )
        close = (100.0 + np.arange(days))[None, :]
        high = close + 7.0
        amount = ((1.0 + np.arange(days)) * 1_000_000)[None, :]
        high[0, 101] = 1_000.0
        for values in (close, high, amount):
            values[0, 110] = np.nan  # The stock did not trade on this calendar day.
        return ExploratoryMarket(
            calendar=dates,
            symbols=("000001",),
            names=("fixture",),
            open=close.copy(),
            high=high,
            low=close - 7.0,
            close=close,
            signal_close=close,
            signal_high=high,
            volume=np.ones_like(close),
            amount=amount,
            industry=(None,),
            fundamental_periods=(),
            fundamentals={
                key: np.empty((1, 0))
                for key in ("roe", "pe", "pb", "net_income_ttm", "net_assets")
            },
        )

    def test_suspension_skips_gap_in_all_candidate_windows(self) -> None:
        candidate = _frame_at(self.market(), 121, {}).candidates[0]

        self.assertAlmostEqual(candidate.ma20, 211.05)
        self.assertAlmostEqual(candidate.ma120, 160.59166666666667)
        self.assertAlmostEqual(candidate.amount20, 112_050_000.0)
        self.assertAlmostEqual(candidate.amount120, 61_591_666.666666664)
        self.assertAlmostEqual(candidate.r20, 0.105)
        self.assertAlmostEqual(candidate.r120, 1.21)
        self.assertAlmostEqual(candidate.drawdown20, -0.779)

    def test_exactly_twenty_bars_has_ma20_but_no_longer_features(self) -> None:
        market = self.market()
        for values in (market.open, market.high, market.low, market.close, market.amount):
            values[0, :101] = np.nan

        candidate = _frame_at(market, 121, {}).candidates[0]
        self.assertAlmostEqual(candidate.ma20, 211.05)
        self.assertAlmostEqual(candidate.amount20, 112_050_000.0)
        self.assertIsNone(candidate.ma120)
        self.assertIsNone(candidate.amount120)
        self.assertIsNone(candidate.r20)
        self.assertIsNone(candidate.r120)

    def test_suspended_signal_day_has_no_stale_candidate_features(self) -> None:
        candidate = _frame_at(self.market(), 110, {}).candidates[0]

        self.assertIsNone(candidate.close)
        self.assertIsNone(candidate.ma20)
        self.assertIsNone(candidate.ma120)
        self.assertIsNone(candidate.amount20)
        self.assertIsNone(candidate.amount120)
        self.assertIsNone(candidate.r20)
        self.assertIsNone(candidate.r120)
        self.assertIsNone(candidate.drawdown20)

    def test_short_history_does_not_read_future_calendar_columns(self) -> None:
        market = self.market()
        before = _frame_at(market, 19, {})
        for values in (market.close, market.high, market.amount):
            values[0, 20:] = 999_999.0
        after = _frame_at(market, 19, {})

        self.assertEqual(before, after)
        self.assertIsNone(after.candidates[0].r20)
        self.assertIsNone(after.candidates[0].r120)

    def test_signals_use_adjusted_prices_but_execution_bar_keeps_raw_prices(self) -> None:
        market = self.market()
        signal_close = market.close.copy() * 2
        signal_high = market.high.copy() * 2
        market = replace(market, signal_close=signal_close, signal_high=signal_high)

        candidate = _frame_at(market, 121, {}).candidates[0]
        self.assertEqual(candidate.close, signal_close[0, 121])
        self.assertAlmostEqual(candidate.ma20, 422.1)
        self.assertEqual(_bar(market, 0, 121).close, market.close[0, 121])

        signal_close[0, 121] = 80.0
        raw, _, _, _ = _market_breadth_state(market)
        self.assertEqual(raw[121], 0.0)


if __name__ == "__main__":
    unittest.main()
