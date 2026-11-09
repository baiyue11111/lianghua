"""Behavior tests for the online rank-ridge strategy."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch

import numpy as np

from core.calculation import Calculation
from plugins.data import Adapter
from protocol import AccountSnapshot, HistoryBar, MarketFrame
from strategy import load_strategy


MODULE = "plugins.strategies.adaptive_rank"


class AdaptiveRankTests(unittest.TestCase):
    def strategy(self, **params):
        return load_strategy(MODULE, params)

    @staticmethod
    def next_day(day):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day.isoformat()

    def frame(self, prices, *, amounts=None, end="2026-06-30", following=None,
              trading=True):
        at_day = datetime.fromisoformat(end).date()
        n = max(map(len, prices.values()))
        days = []
        day = at_day
        while len(days) < n:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.reverse()
        at = datetime.combine(at_day, time(15), tzinfo=ZoneInfo("Asia/Shanghai"))
        history = {}
        fields = {}
        for index, (symbol, values) in enumerate(prices.items()):
            values = [None] * (n - len(values)) + list(values)
            amount_values = (amounts[symbol] if amounts is not None
                             else [100.0 + index * 10.0] * len(prices[symbol]))
            amount_values = [None] * (n - len(amount_values)) + list(amount_values)
            history[symbol] = tuple(
                HistoryBar(datetime.combine(session, time(15), tzinfo=at.tzinfo),
                           {"signal_close": value, "amount": amount})
                for session, value, amount in zip(days, values, amount_values)
            )
            fields[symbol] = {"signal_close": values[-1], "amount": amount_values[-1]}
        return MarketFrame(at, "CN_EQUITY", fields,
                           {"trading": trading,
                            "next_session": following or self.next_day(at_day)}, history)

    def dates(self, start, count):
        day = datetime.fromisoformat(start).date()
        result = []
        while len(result) < count:
            if day.weekday() < 5:
                result.append(day)
            day += timedelta(days=1)
        return result

    def frames(self, prices, *, amounts=None, start="2026-06-24", last_following=None,
               last_trading=True):
        count = max(map(len, prices.values()))
        days = self.dates(start, count)
        result = []
        for index, day in enumerate(days):
            prefix = {symbol: values[:index + 1] for symbol, values in prices.items()}
            amount_prefix = (None if amounts is None else
                             {symbol: values[:index + 1] for symbol, values in amounts.items()})
            following = last_following if index == count - 1 else self.next_day(day)
            result.append(self.frame(prefix, amounts=amount_prefix, end=day.isoformat(), following=following,
                                     trading=last_trading if index == count - 1 else True))
        return result

    def apply(self, strategy, frame):
        return strategy.generate(
            frame, AccountSnapshot(frame.at, Decimal("1000"), Decimal("1000"), {})
        )

    def feed(self, strategy, frames):
        return [self.apply(strategy, frame) for frame in frames]

    def test_requirements_reserve_lookback_horizon_training_and_current_bar(self):
        request = self.strategy().requirements()
        self.assertEqual(request.fields, ("signal_close", "amount"))
        self.assertEqual(request.history_bars, 61)

    def test_online_warmup_and_maturity_boundaries(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=2, positions=1)
        frames = self.frames({"A": [1, 2, 3, 4], "B": [4, 3, 2, 1]},
                             start="2026-06-22")
        counts = []
        outputs = []
        for frame in frames:
            outputs.append(self.apply(strategy, frame))
            counts.append(strategy.sample_days)
        self.assertEqual(counts, [0, 0, 1, 2])
        self.assertEqual(strategy.sample_days, 2)
        self.assertEqual(len(strategy.pending), 1)
        self.assertTrue(all(output is None for output in outputs))

    def test_ridge_solution_matches_hand_calculation(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1, ridge=0.1)
        strategy.xtx = np.diag([1.0, 3.0, 8.0, 2.0, 4.0])
        strategy.xty = np.array([0.4, -0.3, 0.8, -0.6, 0.2])
        self.assertEqual(strategy.xtx.shape, (5, 5))
        strategy.sample_days = 1
        beta = strategy._coefficients()
        expected = np.array([0.4 / 1.1, -0.3 / 3.1, 0.8 / 8.1, -0.6 / 2.1, 0.2 / 4.1])
        np.testing.assert_allclose(beta, expected, rtol=1e-12, atol=1e-12)

    def test_amount_mean_and_cross_sectional_rank(self):
        strategy = self.strategy(lookback=2, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frame = self.frame(
            {"A": [10, 10, 10], "B": [10, 10, 10], "C": [10, 10, 10]},
            amounts={"A": [1, 3, 5], "B": [2, 2, 2], "C": [8, 0, 0]},
        )
        rows, _ = strategy._history(frame)
        raw = strategy._features(rows)
        self.assertEqual(raw["A"]["liquidity"], 4.0)
        self.assertEqual(raw["B"]["liquidity"], 2.0)
        self.assertEqual(raw["C"]["liquidity"], 0.0)
        ranked = strategy._rank(frame, raw, {name: 1.0 for name in
                                               ("reversal", "momentum", "volatility", "liquidity")})
        self.assertEqual(ranked["A"][3], 0.5)
        self.assertEqual(ranked["B"][3], 0.0)
        self.assertEqual(ranked["C"][3], -0.5)

    def test_mature_labels_learn_positive_and_negative_amount_effect(self):
        names = ("A", "B", "C", "D")
        base = {name: [10, 10, end] for name, end in zip(names, (1, 2, 3, 4))}
        amounts = {name: [value, value, value] for name, value in
                   zip(names, (1, 2, 3, 4))}
        params = dict(lookback=1, short_window=1, horizon=1,
                      training_days=1, positions=1, ridge=0.01)

        positive = self.strategy(**params)
        self.feed(positive, self.frames(base, amounts=amounts, start="2026-06-24"))
        positive_beta = positive._coefficients()
        self.assertGreater(positive.xty[3], 0)
        self.assertGreater(positive_beta[3], 0)

        negative_prices = {name: [10, 10, end] for name, end in
                           zip(names, (4, 3, 2, 1))}
        negative = self.strategy(**params)
        self.feed(negative, self.frames(negative_prices, amounts=amounts,
                                        start="2026-06-24"))
        negative_beta = negative._coefficients()
        self.assertLess(negative.xty[3], 0)
        self.assertLess(negative_beta[3], 0)

    def test_amount_none_excludes_symbol_but_zero_is_valid(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frame = self.frame(
            {"A": [10, 10], "B": [10, 10], "C": [10, 10]},
            amounts={"A": [1, None], "B": [1, 0], "C": [1, 2]},
        )
        rows, _ = strategy._history(frame)
        raw = strategy._features(rows)
        self.assertNotIn("A", raw)
        self.assertEqual(raw["B"]["liquidity"], 0.0)
        self.assertIn("B", strategy._rank(
            frame, raw, {name: 1.0 for name in
                         ("reversal", "momentum", "volatility", "liquidity")}
        ))

    def test_amount_requires_registered_values_and_rejects_bad_values(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frame = self.frame({"A": [1, 2], "B": [3, 2], "C": [2, 4]})
        fields = {symbol: dict(row) for symbol, row in frame.fields.items()}
        del fields["A"]["amount"]
        missing_current = MarketFrame(frame.at, frame.venue, fields, frame.context, frame.history)
        with self.assertRaises(ValueError):
            self.apply(strategy, missing_current)
        bad_history = dict(frame.history)
        bad_history["A"] = (*bad_history["A"][:-1], HistoryBar(
            frame.at, {"signal_close": frame.history["A"][-1].fields["signal_close"]}
        ))
        missing_history = MarketFrame(frame.at, frame.venue, frame.fields,
                                      frame.context, bad_history)
        with self.assertRaises(ValueError):
            self.apply(self.strategy(lookback=1, short_window=1, horizon=1,
                                     training_days=1, positions=1), missing_history)
        for value in (True, -1, float("nan"), float("inf"), "bad"):
            with self.subTest(value=value):
                bad = self.frame({"A": [1, 2], "B": [3, 2], "C": [2, 4]},
                                 amounts={"A": [100, value], "B": [100, 200], "C": [100, 300]})
                with self.assertRaises(ValueError):
                    self.apply(self.strategy(lookback=1, short_window=1, horizon=1,
                                             training_days=1, positions=1), bad)

    def test_unused_history_before_feature_window_does_not_change_features(self):
        strategy = self.strategy(lookback=20, short_window=5, horizon=20,
                                 training_days=20, positions=1)
        prices = {symbol: [100 + index + step for step in range(61)]
                  for index, symbol in enumerate(("A", "B", "C"))}
        amounts = {symbol: [1000 + index * 100 + step for step in range(61)]
                   for index, symbol in enumerate(("A", "B", "C"))}
        long_frame = self.frame(prices, amounts=amounts)
        tail_prices = {symbol: values[-21:] for symbol, values in prices.items()}
        tail_amounts = {symbol: values[-21:] for symbol, values in amounts.items()}
        tail_frame = self.frame(tail_prices, amounts=tail_amounts)

        changed = dict(long_frame.history)
        for symbol, bars in changed.items():
            changed[symbol] = tuple(
                HistoryBar(bar.at, {"signal_close": "unused", "amount": "unused"})
                if index < len(bars) - 21 else bar
                for index, bar in enumerate(bars)
            )
        long_frame_with_unused_bad_data = MarketFrame(
            long_frame.at, long_frame.venue, long_frame.fields, long_frame.context, changed
        )
        long_rows, _ = strategy._history(long_frame_with_unused_bad_data)
        tail_rows, _ = strategy._history(tail_frame)
        self.assertEqual(strategy._features(long_rows), strategy._features(tail_rows))
        used_bad = dict(long_frame.history)
        a_bars = list(used_bad["A"])
        bar = a_bars[-21]
        a_bars[-21] = HistoryBar(
            bar.at, {"signal_close": "bad", "amount": bar.fields["amount"]}
        )
        used_bad["A"] = tuple(a_bars)
        invalid_used_window = MarketFrame(
            long_frame.at, long_frame.venue, long_frame.fields, long_frame.context, used_bad
        )
        with self.assertRaises(ValueError):
            strategy._history(invalid_used_window)

    def test_mature_label_enters_once_and_replay_does_not_duplicate(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frames = self.frames({"A": [1, 2, 3], "B": [3, 2, 1], "C": [2, 1, 4]},
                             start="2026-06-24")
        self.feed(strategy, frames)
        self.assertEqual(strategy.sample_days, 1)
        prior_dates = set(strategy.sampled_dates)
        prior_xtx = strategy.xtx.copy()
        self.assertIsNone(self.apply(strategy, frames[-1]))
        self.assertEqual(strategy.sampled_dates, prior_dates)
        np.testing.assert_array_equal(strategy.xtx, prior_xtx)

    def test_feature_rank_pool_is_fixed_before_future_label_arrives(self):
        prices = {
            "A": [1, 2, 1, 2], "B": [2, 1, 2, 1],
            "C": [1, 3, 2, 4], "FUTURE_MISSING": [3, 2, 1, 3],
        }
        baseline = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        missing = self.strategy(lookback=1, short_window=1, horizon=1,
                                training_days=1, positions=1)
        baseline_calls, missing_calls = [], []
        original = Calculation.fundamentals

        def collect(calls):
            def wrapped(frame, *args, **kwargs):
                result = original(frame, *args, **kwargs)
                calls.append((frame.at.date(), result.fields))
                return result
            return wrapped

        baseline_frames = self.frames(prices, start="2026-06-23")
        changed = dict(prices)
        changed["FUTURE_MISSING"] = [3, 2, None, 3]
        missing_frames = self.frames(changed, start="2026-06-23")
        with patch.object(Calculation, "fundamentals", side_effect=collect(baseline_calls)):
            self.feed(baseline, baseline_frames)
        with patch.object(Calculation, "fundamentals", side_effect=collect(missing_calls)):
            self.feed(missing, missing_frames)
        sample_day = baseline_frames[1].at.date()
        before = next(fields for day, fields in baseline_calls
                      if day == sample_day and "reversal" in fields.get("A", {}))
        after = next(fields for day, fields in missing_calls
                     if day == sample_day and "reversal" in fields.get("A", {}))
        self.assertIn("FUTURE_MISSING", before)
        self.assertIn("FUTURE_MISSING", after)
        for symbol in ("A", "B", "C", "FUTURE_MISSING"):
            self.assertEqual(before[symbol], after[symbol])

    def test_unranked_new_issue_does_not_change_mature_label_scale_or_stats(self):
        base = {"A": [1, 2, 3], "B": [3, 2, 1], "C": [2, 1, 4]}
        with_new = {**base, "NEW": [None, 1, 1_000_000]}
        params = dict(lookback=1, short_window=1, horizon=1,
                      training_days=1, positions=1)
        baseline = self.strategy(**params)
        entrant = self.strategy(**params)
        self.feed(baseline, self.frames(base, start="2026-06-24"))
        self.feed(entrant, self.frames(with_new, start="2026-06-24"))
        self.assertEqual(baseline.sampled_dates, entrant.sampled_dates)
        np.testing.assert_allclose(entrant.xtx, baseline.xtx, rtol=0, atol=0)
        np.testing.assert_allclose(entrant.xty, baseline.xty, rtol=0, atol=0)

    def test_market_frame_rejects_future_history(self):
        frame = self.frame({"A": [1, 2, 3], "B": [3, 2, 1], "C": [2, 1, 4]})
        future = HistoryBar(frame.at + timedelta(days=1), {"signal_close": 4.0})
        history = dict(frame.history)
        history["A"] = (*history["A"], future)
        # A frame is a dumb pipe: untrusted input is rejected once at the
        # boundary, matching test_settlement.test_validate_checks_history_*.
        invalid = MarketFrame(frame.at, frame.venue, frame.fields, frame.context, history)
        with self.assertRaises(ValueError):
            Adapter.validate((invalid,))

    def test_missing_prices_are_not_filled_and_invalid_prices_are_rejected(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frames = self.frames({"A": [1, 2, 3, None, 5],
                              "B": [4, 3, 2, 2, 1], "C": [1, 3, 2, 3, 4]},
                             start="2026-06-24", last_following="2026-07-01")
        target = self.feed(strategy, frames)[-1]
        self.assertNotIn("A", target.weights)
        for value in (True, 0, -1, float("nan"), float("inf"), "bad"):
            with self.subTest(value=value):
                bad = self.frame({"A": [1, 2, 3, value], "B": [4, 3, 2, 1],
                                  "C": [1, 3, 2, 4]})
                with self.assertRaises(ValueError):
                    self.apply(self.strategy(lookback=1, short_window=1, horizon=1,
                                             training_days=1, positions=1), bad)

    def test_monthly_target_uses_next_session_and_other_dates_return_none(self):
        prices = {"A": [1, 2, 1, 3, 4], "B": [3, 2, 3, 1, 2], "C": [2, 1, 2, 4, 3]}
        inside = self.strategy(lookback=1, short_window=1, horizon=1,
                               training_days=1, positions=1)
        inside_frames = self.frames(prices, start="2026-06-23", last_following="2026-06-30")
        self.assertIsNone(self.feed(inside, inside_frames)[-1])
        month_end = self.strategy(lookback=1, short_window=1, horizon=1,
                                  training_days=1, positions=1)
        month_frames = self.frames(prices, start="2026-06-24", last_following="2026-07-01")
        target = self.feed(month_end, month_frames)[-1]
        self.assertIsNotNone(target)
        self.assertEqual(target.as_of, month_frames[-1].at)
        self.assertEqual(sum(target.weights.values()), 1.0)

    def test_nontrading_frame_warms_model_without_emitting_target(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=2, positions=1)
        frames = self.frames({"A": [1, 2, 3, 4], "B": [4, 3, 2, 1]},
                             start="2026-06-22", last_trading=False)
        output = self.feed(strategy, frames)[-1]
        self.assertIsNone(output)
        self.assertEqual(strategy.sample_days, 2)

    def test_zero_prediction_returns_empty_target(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=20)
        frames = self.frames({"A": [5] * 5, "B": [5] * 5, "C": [5] * 5},
                             start="2026-06-24", last_following="2026-07-01")
        target = self.feed(strategy, frames)[-1]
        self.assertEqual(dict(target.weights), {})

    def test_invalid_short_window_is_rejected(self):
        with self.assertRaises(ValueError):
            self.strategy(lookback=1, short_window=2)

    def test_max_return_uses_exact_window_and_missing_is_excluded(self):
        strategy = self.strategy(lookback=3, short_window=1)
        frame = self.frame({'A': [.5, 2, 3, 3, 6], 'B': [10, 10, None, 10, 11],
                            'C': [10, 10, 9, 8, 7]})
        rows, _ = strategy._history(frame)
        features = strategy._features(rows)
        self.assertEqual(features['A']['max_return'], 1.0)
        self.assertAlmostEqual(features['C']['max_return'], -.1)
        self.assertNotIn('B', features)

    def test_max_return_ties_use_average_rank(self):
        strategy = self.strategy(lookback=2, short_window=1)
        frame = self.frame({'A': [10, 15, 15], 'B': [10, 10, 15], 'C': [10, 20, 20]})
        rows, _ = strategy._history(frame)
        raw = strategy._features(rows)
        self.assertEqual(raw['A']['max_return'], .5)
        self.assertEqual(raw['B']['max_return'], .5)
        ranks = strategy._rank(frame, raw, {'max_return': 1.0})
        self.assertEqual(ranks['A'][0], -.25)
        self.assertEqual(ranks['B'][0], -.25)
        self.assertEqual(ranks['C'][0], .5)

    def test_max_return_frozen_when_future_label_missing(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1, training_days=1)
        frames = self.frames({'A': [1, 2, None], 'B': [2, 1, 2],
                              'C': [1, 3, 2]}, start='2026-06-23')
        self.feed(strategy, frames[:2])
        sample = strategy.pending[frames[1].at.date()]['features']
        self.assertEqual(len(sample['A']), 5)
        frozen = np.stack([sample['B'].copy(), sample['C'].copy()])
        self.apply(strategy, frames[2])
        np.testing.assert_allclose(strategy.xtx, frozen.T @ frozen / 2)
        self.assertEqual(strategy.sample_days, 1)

    def test_weekly_requirements_and_parameter_validation(self):
        self.assertEqual(self.strategy(horizon=5, rebalance_period='weekly').requirements().history_bars, 46)
        self.assertEqual(self.strategy().requirements().history_bars, 61)
        for value in ('daily', '', True, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.strategy(rebalance_period=value)

    def test_weekly_boundary_uses_next_session_and_iso_week(self):
        cases = [('2026-02-18', '2026-02-23', True),
                 ('2019-12-30', '2020-01-02', False),
                 ('2020-12-31', '2021-01-04', True),
                 ('2026-06-29', '2026-06-30', False)]
        for day, following, expected in cases:
            with self.subTest(day=day, following=following):
                strategy = self.strategy(lookback=1, short_window=1, horizon=5,
                                         rebalance_period='weekly')
                frame = self.frame({'A': [1, 2], 'B': [2, 1]}, end=day, following=following)
                with patch.object(strategy, '_coefficients', return_value=np.array([1., 1., 1., 1., 0.])):
                    result = self.apply(strategy, frame)
                self.assertEqual(result is not None, expected)

    def test_weekly_invalid_next_session_rejected(self):
        frame = self.frame({'A': [1, 2]}, following='2026-06-30')
        for context in ({'trading': True}, {'trading': True, 'next_session': 'bad'},
                        {'trading': True, 'next_session': '2026-06-29'},
                        {'trading': True, 'next_session': '2026-06-30'}):
            strategy = self.strategy(lookback=1, short_window=1, horizon=5, rebalance_period='weekly')
            invalid = MarketFrame(frame.at, frame.venue, frame.fields, context, frame.history)
            with self.subTest(context=context), self.assertRaises((ValueError, TypeError)):
                self.apply(strategy, invalid)

    def test_five_session_maturity_and_replay(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=5,
                                 training_days=1, rebalance_period='weekly')
        frames = self.frames({'A': [1, 2, 3, 4, 5, 6, 7],
                              'B': [7, 6, 5, 4, 3, 2, 1]}, start='2026-06-22')
        counts = []
        for frame in frames:
            self.apply(strategy, frame)
            counts.append(strategy.sample_days)
        self.assertEqual(counts, [0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(strategy.sampled_dates, {frames[1].at.date()})
        before = strategy.xtx.copy()
        self.assertIsNone(self.apply(strategy, frames[-1]))
        np.testing.assert_array_equal(strategy.xtx, before)

    def test_weekly_account_does_not_change_targets(self):
        frame = self.frame({'A': [1, 2], 'B': [2, 1]}, end='2026-02-18', following='2026-02-23')
        params = dict(lookback=1, short_window=1, horizon=5, rebalance_period='weekly')
        targets = []
        for positions in ({}, {'A': Decimal('100')}):
            strategy = self.strategy(**params)
            account = AccountSnapshot(frame.at, Decimal('1000'), Decimal('1000'), positions)
            with patch.object(strategy, '_coefficients', return_value=np.array([1., 1., 1., 1., 0.])):
                targets.append(strategy.generate(frame, account))
        self.assertEqual(targets[0], targets[1])

    def test_five_session_missing_label_excluded_without_reranking_saved_features(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=5,
                                 training_days=1, rebalance_period='weekly')
        frames = self.frames({'A': [2, 3, 4, 5, 6, 7, None],
                              'B': [4, 3, 3, 3, 3, 3, 2],
                              'C': [2, 1, 1, 1, 1, 1, 3]}, start='2026-06-22')
        self.feed(strategy, frames[:2])
        sample = strategy.pending[frames[1].at.date()]['features']
        expected_x = np.stack([sample['B'], sample['C']])
        self.feed(strategy, frames[2:])
        np.testing.assert_allclose(strategy.xtx, expected_x.T @ expected_x / 2)
        self.assertEqual(strategy.sample_days, 1)

    def test_weekly_nontrading_warmup_matures_without_target(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=5,
                                 training_days=1, rebalance_period='weekly')
        frames = self.frames({'A': [1, 2, 3, 4, 5, 6, 7],
                              'B': [7, 6, 5, 4, 3, 2, 1]}, start='2026-06-22',
                             last_trading=False)
        self.assertIsNone(self.feed(strategy, frames)[-1])
        self.assertEqual(strategy.sample_days, 1)

    def test_input_histories_and_context_are_unchanged(self):
        strategy = self.strategy(lookback=1, short_window=1, horizon=1,
                                 training_days=1, positions=1)
        frame = self.frame({"A": [1, 2, 1, 3], "B": [3, 2, 3, 1], "C": [2, 1, 2, 4]})
        before_history = {symbol: tuple(rows) for symbol, rows in frame.history.items()}
        before_context = dict(frame.context)
        self.apply(strategy, frame)
        self.assertEqual(dict(frame.history), before_history)
        self.assertEqual(dict(frame.context), before_context)


if __name__ == "__main__":
    unittest.main()
