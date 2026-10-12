"""Regression tests for own-trading-day rolling features.

These lock down the bug that silently zeroed out market breadth: a
cumsum-based rolling mean propagates a single NaN over the whole remaining
history, so any symbol that IPO'd late or was ever suspended lost its MA120.
"""

from __future__ import annotations

import unittest

import numpy as np

from panel_features import (
    own_day_lagged_return,
    own_day_rolling_mean,
    trailing_mean,
    valid_mask,
)

NAN = float("nan")


class OwnDayRollingMeanTests(unittest.TestCase):
    def test_dense_row_matches_plain_rolling_mean(self) -> None:
        values = np.array([[1.0, 2.0, 3.0, 4.0, 5.0]])
        out = own_day_rolling_mean(values, 3)
        np.testing.assert_allclose(out[0][2:], [2.0, 3.0, 4.0])
        self.assertTrue(np.isnan(out[0][:2]).all())

    def test_leading_nan_does_not_kill_the_rest_of_history(self) -> None:
        """The exact failure mode of the cumsum implementation."""
        values = np.array([[NAN, NAN, 1.0, 2.0, 3.0, 4.0, 5.0]])
        out = own_day_rolling_mean(values, 3)
        # first two columns are pre-IPO -> NaN, and so is the first bar with
        # fewer than 3 own observations, but the tail must be finite.
        np.testing.assert_allclose(out[0][4:], [2.0, 3.0, 4.0])
        self.assertTrue(np.isnan(out[0][:4]).all())

    def test_interior_gap_is_skipped_not_zero_filled(self) -> None:
        # Suspension on column 2. The window must use own bars 1,2,4 -> 1,2,3.
        values = np.array([[1.0, 2.0, NAN, 3.0, 4.0]])
        out = own_day_rolling_mean(values, 3)
        self.assertTrue(np.isnan(out[0][2]))  # not a bar -> no value
        self.assertAlmostEqual(out[0][3], (1.0 + 2.0 + 3.0) / 3)
        self.assertAlmostEqual(out[0][4], (2.0 + 3.0 + 4.0) / 3)

    def test_row_shorter_than_window_is_all_nan(self) -> None:
        values = np.array([[1.0, 2.0, NAN, NAN]])
        out = own_day_rolling_mean(values, 3)
        self.assertTrue(np.isnan(out).all())

    def test_rows_are_independent(self) -> None:
        values = np.array(
            [
                [1.0, 2.0, 3.0, 4.0],
                [NAN, 10.0, 20.0, 30.0],
            ]
        )
        out = own_day_rolling_mean(values, 2)
        np.testing.assert_allclose(out[0][1:], [1.5, 2.5, 3.5])
        np.testing.assert_allclose(out[1][2:], [15.0, 25.0])
        self.assertTrue(np.isnan(out[1][:2]).all())

    def test_explicit_mask_overrides_finiteness(self) -> None:
        values = np.array([[1.0, 2.0, 3.0, 4.0]])
        mask = np.array([[True, False, True, True]])
        out = own_day_rolling_mean(values, 2, mask=mask)
        self.assertTrue(np.isnan(out[0][1]))
        self.assertAlmostEqual(out[0][2], 2.0)  # (1 + 3) / 2
        self.assertAlmostEqual(out[0][3], 3.5)  # (3 + 4) / 2


class OwnDayLaggedReturnTests(unittest.TestCase):
    def test_dense_row(self) -> None:
        values = np.array([[10.0, 11.0, 12.0, 13.0]])
        out = own_day_lagged_return(values, 2)
        self.assertTrue(np.isnan(out[0][:2]).all())
        self.assertAlmostEqual(out[0][2], 12.0 / 10.0 - 1)
        self.assertAlmostEqual(out[0][3], 13.0 / 11.0 - 1)

    def test_gap_counts_own_bars_only(self) -> None:
        values = np.array([[10.0, NAN, 11.0, 12.0]])
        out = own_day_lagged_return(values, 2)
        self.assertTrue(np.isnan(out[0][1]))
        self.assertTrue(np.isnan(out[0][2]))  # only 2 own bars so far
        self.assertAlmostEqual(out[0][3], 12.0 / 10.0 - 1)


class ValidMaskTests(unittest.TestCase):
    def test_requires_all_arrays_finite_and_positive(self) -> None:
        a = np.array([[1.0, 2.0, NAN, 4.0]])
        b = np.array([[1.0, 0.0, 3.0, 4.0]])
        np.testing.assert_array_equal(
            valid_mask(a, b), np.array([[True, False, False, True]])
        )


class TrailingMeanTests(unittest.TestCase):
    def test_window_and_warmup(self) -> None:
        s = np.array([1.0, 2.0, 3.0, 4.0])
        out = trailing_mean(s, 2)
        self.assertTrue(np.isnan(out[0]))
        np.testing.assert_allclose(out[1:], [1.5, 2.5, 3.5])

    def test_nan_inside_window_yields_nan_but_recovers(self) -> None:
        s = np.array([1.0, NAN, 3.0, 4.0])
        out = trailing_mean(s, 2)
        self.assertTrue(np.isnan(out[1]))
        self.assertTrue(np.isnan(out[2]))
        self.assertAlmostEqual(out[3], 3.5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
