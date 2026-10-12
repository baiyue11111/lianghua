"""Protect causal replay and pause/recovery using hand-controlled returns."""
from datetime import date, timedelta
import unittest

from dynamic_selection import GatePolicy, ShadowReturn, replay_states


class DynamicSelectionTests(unittest.TestCase):
    def setUp(self):
        self.policy = GatePolicy(lookback=3, confirmations=2, min_active_days=0,
                                 entry_sharpe=1, exit_sharpe=0, max_drawdown=0.8)
        values = [.01, .02, .01, .02, -.04, -.03, -.04, -.03, .01, .02, .01, .02]
        self.points = tuple(ShadowReturn(date(2020, 1, 1)+timedelta(days=i), r)
                            for i, r in enumerate(values))

    def test_same_strategy_pauses_and_recovers_on_shadow_history(self):
        events = replay_states(self.points, self.policy, qualified_on=date(2019, 1, 1))
        self.assertEqual(events[3].state, "active")
        self.assertEqual(events[7].state, "paused")
        self.assertEqual(events[-1].state, "active")
        self.assertEqual(events[-1].transition, "resumed")

    def test_future_returns_do_not_change_any_prior_decision(self):
        short = replay_states(self.points[:7], self.policy, qualified_on=date(2019, 1, 1))
        full = replay_states(self.points, self.policy, qualified_on=date(2019, 1, 1))
        self.assertEqual(short, full[:7])

    def test_historical_qualification_cannot_be_backdated(self):
        events = replay_states(self.points, self.policy, qualified_on=self.points[-1].day)
        self.assertTrue(all(e.state == "watch" for e in events[:-1]))
        self.assertNotEqual(events[-1].state, "active")

    def test_unqualified_strategy_never_activates(self):
        events = replay_states(self.points, self.policy, qualified_on=None)
        self.assertTrue(all(e.state == "watch" for e in events))

    def test_duplicate_dates_and_invalid_returns_are_rejected(self):
        with self.assertRaises(ValueError):
            replay_states((self.points[0], self.points[0]), self.policy, qualified_on=None)
        with self.assertRaises(ValueError):
            ShadowReturn(date(2020, 1, 1), float("nan"))

    def test_single_losing_day_does_not_bypass_confirmation(self):
        events = replay_states(self.points[:5], self.policy, qualified_on=date(2019, 1, 1))
        self.assertEqual(events[-1].state, "active")

    def test_zero_variance_is_not_an_infinite_sharpe(self):
        points = tuple(ShadowReturn(p.day, .01) for p in self.points)
        events = replay_states(points, self.policy, qualified_on=date(2019, 1, 1))
        self.assertTrue(all(e.state == "watch" for e in events))

    def test_hard_drawdown_exit_is_immediate_even_during_minimum_hold(self):
        policy = GatePolicy(lookback=3, confirmations=2, min_active_days=100,
                            entry_sharpe=1, exit_sharpe=0, max_drawdown=.02)
        events = replay_states(self.points[:5], policy, qualified_on=date(2019, 1, 1))
        self.assertEqual(events[-2].state, "active")
        self.assertEqual(events[-1].state, "paused")
        self.assertEqual(events[-1].reason, "drawdown_limit")
