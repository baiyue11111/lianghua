"""Public formal-strategy contract tests migrated from the removed strategy port."""
from datetime import datetime
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from core.calculation import AccountExecutionSession
from core.calculation import AccountLedger
from protocal import AccountSnapshot, HistoryBar, MarketFrame
from strategy import catalog, load_strategy
from tests.fee_fixture import rules


MODULE = "plugins.strategies.moving_average_cross"


class StrategyPortTests(unittest.TestCase):
    def frame(self, prices, *, trading=True):
        start = datetime(2023, 1, 1, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = {
            symbol: tuple(
                HistoryBar(start.replace(day=start.day + index), {"signal_close": value})
                for index, value in enumerate(values)
            )
            for symbol, values in prices.items()
        }
        at = max(row.at for rows in history.values() for row in rows)
        return MarketFrame(at, "CN_EQUITY", {}, {"trading": trading}, history)

    def generate(self, strategy, prices, **kwargs):
        frame = self.frame(prices, **kwargs)
        return strategy.generate(
            frame, AccountSnapshot(frame.at, Decimal("1000"), Decimal("1000"), {})
        )

    def test_formal_params_requirements_and_generate_use_one_plugin_interface(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 3})
        self.assertEqual(strategy.strategy_id, "moving_average_cross")
        self.assertEqual(strategy.requirements().fields, ("signal_close",))
        target = self.generate(strategy, {"x": [1, 2, 3]})
        self.assertEqual(dict(target.weights), {"x": 1.0})

    def test_none_decision_keeps_pending_orders(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 3, "exposure": .4})
        target = self.generate(strategy, {"x": [1, 2, 3]})
        session = AccountExecutionSession(AccountLedger(Decimal("100000")), rules())
        session.apply_target(
            target,
            next_session=target.as_of.date().replace(day=target.as_of.day + 1),
            equity=Decimal("100000"),
            raw_closes={"x": Decimal("10")},
            amount20={"x": Decimal("50000000")},
        )
        self.assertEqual(len(session.pending), 1)
        self.assertIsNone(self.generate(strategy, {"x": [1, 2, 3]}))
        self.assertEqual(len(session.pending), 1)

    def test_unknown_and_coerced_parameters_are_rejected(self):
        for params in ({"unknown": 1}, {"fast_window": "2"}, {"fast_window": True}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                load_strategy(MODULE, params)

    def test_windows_change_requirements_and_identity_is_stable(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 5})
        self.assertEqual(strategy.requirements().history_bars, 5)
        self.assertEqual(strategy.requirements().frequency, "daily")
        self.assertEqual(strategy.requirements().price_basis, "adjusted")
        self.assertEqual(strategy.strategy_id, "moving_average_cross")

    def test_empty_target_is_not_none(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 3})
        self.generate(strategy, {"x": [1, 2, 3]})
        self.assertIsNone(self.generate(strategy, {"x": [1, 2, 3]}))
        target = self.generate(strategy, {"x": [3, 2, 1]})
        self.assertIsNotNone(target)
        self.assertEqual(dict(target.weights), {})

    def test_risk_exposure_changes_weights_but_keeps_signal_reason(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 3, "exposure": .8})
        target = self.generate(strategy, {"x": [1, 2, 3], "y": [3, 2, 1], "z": [2, 3, 4]})
        self.assertEqual(dict(target.weights), {"x": .4, "z": .4})
        self.assertEqual(set(target.reasons.values()), {"fast_mean_above_slow_mean"})

    def test_fixed_slot_default_remains_eight(self):
        item = next(
            item for item in catalog()
            if item["module"] == "plugins.strategies.breadth_low_amount_reversal"
        )
        self.assertEqual(item["defaults"]["positions"], 8)

    def test_raw_history_window_controls_warmup(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 4})
        self.assertIsNone(self.generate(strategy, {"x": [1, 2, 3]}))
        self.assertEqual(dict(self.generate(strategy, {"x": [1, 2, 3, 4]}).weights), {"x": 1.0})

    def test_formal_target_is_immutable_after_input_mapping_changes(self):
        strategy = load_strategy(MODULE, {"fast_window": 2, "slow_window": 3})
        target = self.generate(strategy, {"x": [1, 2, 3]})
        original = dict(target.weights)
        self.assertEqual(dict(target.weights), original)
        self.assertEqual(dict(target.reasons), {"x": "fast_mean_above_slow_mean"})


if __name__ == "__main__":
    unittest.main()
