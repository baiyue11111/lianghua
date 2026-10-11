"""Own-trading-day rolling features for a wide (symbol x calendar-day) panel.

Why this module exists
----------------------
A naive ``np.cumsum``-based rolling mean is WRONG for an A-share panel:

  * 58% of the 5,162 symbols have no data on the first calendar column
    (they IPO'd after 2014-01-02), and 2,921 symbols carry interior
    suspension gaps (266,435 NaN days in total).
  * ``np.cumsum`` propagates a single NaN to every later element, so the
    prefix-sum difference is NaN for the entire remaining history.

Consequence measured on the real panel: a cumsum MA120 was entirely NaN for
3,700 / 5,162 symbols, ``close > ma120`` was therefore ``False`` for 71.9% of
symbols over their whole life, and market breadth collapsed to a 5.7% mean
(never reaching the strategy's 60% entry gate). See tests/test_panel_features.py.

Semantics implemented here
--------------------------
Every window is measured over the symbol's OWN valid trading days, which is
how MA120 / 20-day return are defined on every trading platform: a suspension
(停牌) day is not a bar, it is skipped, not treated as a zero or as a gap.
Results are scattered back onto the shared calendar so that column ``t`` always
means the same date for every symbol.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "valid_mask",
    "own_day_rolling_mean",
    "own_day_lagged_return",
    "own_day_count",
    "trailing_mean",
]


def valid_mask(*arrays: np.ndarray, positive: bool = True) -> np.ndarray:
    """Element-wise "this symbol really traded on this day" mask.

    A day counts only when every supplied array is finite there (and strictly
    positive when ``positive``), which is what makes a bar tradable.
    """
    if not arrays:
        raise ValueError("valid_mask needs at least one array")
    mask = np.ones(arrays[0].shape, dtype=bool)
    for a in arrays:
        mask &= np.isfinite(a)
        if positive:
            mask &= a > 0
    return mask


def own_day_rolling_mean(
    values: np.ndarray, window: int, mask: np.ndarray | None = None
) -> np.ndarray:
    """Rolling mean over each row's own valid observations.

    ``out[i, t]`` is the mean of the ``window`` most recent valid observations
    of row ``i`` up to and including column ``t`` (inclusive), or NaN when
    column ``t`` is not a valid observation or fewer than ``window`` valid
    observations exist up to it. NaNs never leak forward.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    values = np.asarray(values, dtype=np.float64)
    if mask is None:
        mask = np.isfinite(values)
    out = np.full(values.shape, np.nan, dtype=np.float64)
    for i in range(values.shape[0]):
        m = mask[i]
        k = int(m.sum())
        if k < window:
            continue
        v = values[i][m]
        if not np.isfinite(v).all():
            raise ValueError(f"row {i}: mask admits a non-finite value")
        cs = np.empty(k + 1, dtype=np.float64)
        cs[0] = 0.0
        np.cumsum(v, out=cs[1:])
        buf = np.full(k, np.nan, dtype=np.float64)
        buf[window - 1 :] = (cs[window:] - cs[:-window]) / window
        out[i][m] = buf
    return out


def own_day_lagged_return(
    values: np.ndarray, window: int, mask: np.ndarray | None = None
) -> np.ndarray:
    """``values[t] / values[t - window] - 1`` measured in own trading days."""
    if window < 1:
        raise ValueError("window must be >= 1")
    values = np.asarray(values, dtype=np.float64)
    if mask is None:
        mask = np.isfinite(values) & (values > 0)
    out = np.full(values.shape, np.nan, dtype=np.float64)
    for i in range(values.shape[0]):
        m = mask[i]
        k = int(m.sum())
        if k <= window:
            continue
        v = values[i][m]
        buf = np.full(k, np.nan, dtype=np.float64)
        buf[window:] = v[window:] / v[:-window] - 1.0
        out[i][m] = buf
    return out


def own_day_count(mask: np.ndarray) -> np.ndarray:
    """Cumulative count of a row's own valid observations, inclusive of ``t``."""
    return np.cumsum(mask.astype(np.int32), axis=1)


def trailing_mean(series: np.ndarray, window: int) -> np.ndarray:
    """Simple trailing mean of a 1-D series with a NaN warm-up prefix.

    Used for the 5-day breadth smoothing, where the input is a single
    market-wide series that is dense once the warm-up has passed.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    series = np.asarray(series, dtype=np.float64)
    out = np.full(series.shape, np.nan, dtype=np.float64)
    n = series.shape[0]
    for t in range(window - 1, n):
        w = series[t - window + 1 : t + 1]
        if np.isfinite(w).all():
            out[t] = w.mean()
    return out
