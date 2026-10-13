"""Small, deterministic calculations shared by production strategies and runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite, sqrt
from statistics import fmean, stdev

from protocal import Trial


def _candidate_parameters(candidate: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(candidate, Mapping):
        raise TypeError("each candidate must be a mapping")
    if not candidate:
        raise ValueError("candidate must not be empty")
    item = {}
    for key, value in candidate.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("candidate parameter names must be non-empty strings")
        if type(value) not in (str, int, float, bool, type(None)):
            raise TypeError("candidate parameter values must be scalars")
        if type(value) is float and not isfinite(value):
            raise ValueError("candidate parameter values must be finite")
        item[key] = value
    return item


class Calculation:
    @staticmethod
    def propose(trials: tuple[Trial, ...], *, candidates: Sequence[Mapping[str, object]]):
        if not isinstance(trials, tuple) or any(not isinstance(trial, Trial) for trial in trials):
            raise TypeError("trials must be a tuple of Trial values")
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a non-empty sequence")
        if not candidates:
            raise ValueError("candidates must not be empty")
        if len(trials) >= len(candidates):
            return None
        candidate = candidates[len(trials)]
        try:
            return _candidate_parameters(candidate)
        except (TypeError, ValueError) as error:
            error.failed_values = candidate
            error.trial_index = len(trials) + 1
            raise
    @staticmethod
    def _finite(value: float, name: str) -> float:
        try:
            finite = isfinite(value)
        except (OverflowError, TypeError) as error:
            raise ValueError(f"{name} must be finite") from error
        if not finite:
            raise ValueError(f"{name} must be finite")
        return value

    @staticmethod
    def sma(values: Sequence[int | float], window: int) -> float:
        values = tuple(values)
        if type(window) is not int or window < 1 or window > len(values):
            raise ValueError("window must be a positive integer no larger than values")
        window_values = values[-window:]
        for value in window_values:
            if type(value) not in (int, float):
                raise ValueError("values must be finite numbers")
            Calculation._finite(value, "values")
        try:
            result = sum(window_values) / window
        except OverflowError as error:
            raise ValueError("sma result must be finite") from error
        return Calculation._finite(result, "sma result")

    @staticmethod
    def equal_weights(
        symbols: Sequence[str], slots: int, gross: int | float = 1.0,
    ) -> dict[str, float]:
        symbols = tuple(symbols)
        if (type(slots) is not int or slots < 1 or len(set(symbols)) != len(symbols)
                or len(symbols) > slots
                or any(type(symbol) is not str or not symbol.strip() for symbol in symbols)):
            raise ValueError("unique symbols within positive slots required")
        if type(gross) not in (int, float) or not 0 <= gross <= 1:
            raise ValueError("gross must be finite in [0, 1]")
        Calculation._finite(gross, "gross")
        return {symbol: gross / slots for symbol in symbols}

    @staticmethod
    def evaluate(
        points: Sequence[tuple[date, int | float]], *,
        periods: int = 252, baseline: int | float = 0.0,
    ) -> dict[str, float | int | None]:
        points = tuple(points)
        if not points:
            raise ValueError("equity points are required")
        if type(periods) is not int or periods < 1:
            raise ValueError("periods must be a positive integer")
        if type(baseline) not in (int, float):
            raise ValueError("baseline must be a finite number")
        Calculation._finite(baseline, "baseline")
        previous_day = None
        equity = []
        for point in points:
            if not isinstance(point, Sequence) or len(point) != 2:
                raise TypeError("equity points must be (date, value) pairs")
            day, value = point
            if not isinstance(day, date):
                raise TypeError("equity point dates are required")
            if previous_day is not None and day <= previous_day:
                raise ValueError("equity point dates must be strictly increasing")
            if type(value) not in (int, float) or value <= 0:
                raise ValueError("equity values must be finite and positive")
            Calculation._finite(value, "equity value")
            previous_day = day
            equity.append(value)
        try:
            returns = [equity[index] / equity[index - 1] - 1 for index in range(1, len(equity))]
        except OverflowError as error:
            raise ValueError("returns must be finite") from error
        for value in returns:
            Calculation._finite(value, "returns")
        peaks: list[float] = []
        current_peak = equity[0]
        for value in equity:
            current_peak = max(current_peak, value)
            peaks.append(current_peak)
        drawdowns = [value / peak - 1 for value, peak in zip(equity, peaks, strict=True)]
        years = max((points[-1][0] - points[0][0]).days / 365.25, 1 / periods)
        try:
            cagr = (equity[-1] / equity[0]) ** (1 / years) - 1
        except (OverflowError, ZeroDivisionError) as error:
            raise ValueError("cagr must be finite") from error
        Calculation._finite(cagr, "cagr")
        if len(returns) < 2:
            sharpe: float | None = None
            volatility: float | None = None
        else:
            try:
                deviation = stdev(returns)
            except (OverflowError, ValueError) as error:
                raise ValueError("return deviation must be finite") from error
            Calculation._finite(deviation, "return deviation")
            if deviation == 0:
                sharpe = None
                volatility = None
            else:
                volatility = deviation * sqrt(periods)
                excess = [value - baseline for value in returns]
                for value in excess:
                    Calculation._finite(value, "excess return")
                try:
                    sharpe = fmean(excess) / deviation * sqrt(periods)
                except (OverflowError, ZeroDivisionError) as error:
                    raise ValueError("sharpe must be finite") from error
        result = {
            "initial_equity": equity[0],
            "final_equity": equity[-1],
            "total_return": equity[-1] / equity[0] - 1,
            "cagr": cagr,
            "annualized_volatility": volatility,
            "sharpe": sharpe,
            "max_drawdown": min(drawdowns),
            "observations": len(points),
        }
        for name, value in result.items():
            if value is not None:
                Calculation._finite(value, name)
        return result


__all__ = ["Calculation"]
