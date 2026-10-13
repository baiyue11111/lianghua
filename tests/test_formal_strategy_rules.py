"""Current formal proxy strategy rules, migrated from the retired strategy chain."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from protocal import AccountSnapshot, HistoryBar, MarketFrame
from strategy import load_strategy


class FormalStrategyRuleTests(unittest.TestCase):
    def frame(self, values, *, trading=True):
        start = datetime(2024, 1, 2, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = {
            symbol: tuple(
                HistoryBar(start + timedelta(days=index), {"signal_close": value})
                for index, value in enumerate(prices)
            )
            for symbol, prices in values.items()
        }
        at = max(row.at for rows in history.values() for row in rows)
        return MarketFrame(
            at, "CN_EQUITY", {},
            {"trading": trading, "next_session": (at + timedelta(days=1)).date().isoformat()},
            history,
        )

    def account(self, frame):
        return AccountSnapshot(frame.at, Decimal("100000"), Decimal("100000"), {})

    def test_era_leadership_uses_momentum_sort_and_slots(self):
        strategy = load_strategy(
            "plugins.strategies.era_leadership",
            {"positions": 2, "momentum_window": 20, "exposure": .5},
        )
        frame = self.frame({"A": [1] * 19 + [4], "B": [1] * 19 + [.5],
                            "C": [1] * 19 + [5]})
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": .25, "C": .25})
        self.assertEqual(set(target.reasons.values()), {"highest_momentum"})

    def test_quality_trend_uses_fast_over_slow_sort_and_slots(self):
        strategy = load_strategy(
            "plugins.strategies.quality_trend",
            {"positions": 1, "fast_window": 2, "slow_window": 20, "exposure": 1.0},
        )
        frame = self.frame({"A": [1] * 19 + [2], "B": [1] * 19 + [1.5],
                            "C": [1] * 20})
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": 1.0})
        self.assertEqual(dict(target.reasons), {"A": "fast_above_slow"})

    def test_trend_pullback_requires_trend_and_drawdown(self):
        strategy = load_strategy(
            "plugins.strategies.trend_pullback",
            {"positions": 1, "trend_window": 20, "pullback_window": 2,
             "drawdown": .08, "exposure": 1.0},
        )
        frame = self.frame({"A": [10, 14] + [10] * 17 + [11, 12.5],
                            "B": [10] * 18 + [11, 12]})
        target = strategy.generate(frame, self.account(frame))
        self.assertEqual(dict(target.weights), {"A": 1.0})
        self.assertEqual(dict(target.reasons), {"A": "trend_then_pullback"})

    def test_formal_proxy_strategies_return_none_outside_trading_phase(self):
        values = {"A": [1] * 19 + [2]}
        cases = (
            ("plugins.strategies.era_leadership", {"positions": 1, "momentum_window": 20}),
            ("plugins.strategies.quality_trend", {"positions": 1, "fast_window": 2, "slow_window": 20}),
            ("plugins.strategies.trend_pullback", {"positions": 1, "trend_window": 20, "pullback_window": 2}),
        )
        for module, params in cases:
            with self.subTest(module=module):
                strategy = load_strategy(module, params)
                frame = self.frame(values, trading=False)
                self.assertIsNone(strategy.generate(frame, self.account(frame)))


if __name__ == "__main__":
    unittest.main()
