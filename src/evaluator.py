"""Deterministic performance statistics over the immutable equity ledger."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import sqrt
from statistics import fmean, stdev


def evaluate_equity(points: Sequence[tuple[date, float]]) -> dict[str, float | int | None]:
    if not points:
        raise ValueError("equity points are required")
    equity = [value for _, value in points]
    returns = [equity[index] / equity[index - 1] - 1 for index in range(1, len(equity))]
    peaks: list[float] = []
    current_peak = equity[0]
    for value in equity:
        current_peak = max(current_peak, value)
        peaks.append(current_peak)
    drawdowns = [value / peak - 1 for value, peak in zip(equity, peaks, strict=True)]
    years = max((points[-1][0] - points[0][0]).days / 365.25, 1 / 252)
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1
    if len(returns) < 2 or stdev(returns) == 0:
        sharpe: float | None = None
        volatility: float | None = None
    else:
        volatility = stdev(returns) * sqrt(252)
        sharpe = fmean(returns) / stdev(returns) * sqrt(252)
    return {
        "initial_equity": equity[0],
        "final_equity": equity[-1],
        "total_return": equity[-1] / equity[0] - 1,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": min(drawdowns),
        "observations": len(points),
    }
