"""Walk-forward-ready exploratory backtest runner over normalized Parquet panels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyarrow.parquet as pq

from contracts import Bar, Candidate, Fill, Order, Side, StrategyFrame, TargetPortfolio
from execution import ExecutionEngine, PositionBook, remaining_order
from features import conservative_available_date
from panel_features import own_day_rolling_mean, valid_mask


class Strategy(Protocol):
    strategy_id: str

    def target(self, frame: StrategyFrame) -> TargetPortfolio: ...


@dataclass(frozen=True)
class ExploratoryMarket:
    calendar: tuple[date, ...]
    symbols: tuple[str, ...]
    names: tuple[str, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    signal_close: np.ndarray
    signal_high: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    industry: tuple[str | None, ...]
    fundamental_periods: tuple[str, ...]
    fundamentals: dict[str, np.ndarray]

    @classmethod
    def load(cls, data_dir: Path) -> "ExploratoryMarket":
        calendar = tuple(pq.read_table(data_dir / "calendar.parquet").column("trade_date").to_pylist())
        price_path = data_dir / "price_panels.parquet"
        if price_path.exists():
            prices = pq.read_table(price_path)
        else:
            prices = pq.read_table(data_dir / "price_panels")
        fundamentals = pq.read_table(data_dir / "fundamental_panels.parquet")
        raw_symbols = prices.column("symbol").to_pylist()
        _, unique_indices = np.unique(np.asarray(raw_symbols, dtype=object), return_index=True)
        unique_indices = np.sort(unique_indices)
        price_symbols = tuple(raw_symbols[index] for index in unique_indices)
        fundamental_by_symbol = {symbol: index for index, symbol in enumerate(fundamentals.column("symbol").to_pylist())}
        aligned = [fundamental_by_symbol.get(symbol) for symbol in price_symbols]
        if any(index is None for index in aligned):
            raise ValueError("each price symbol must have a fundamental row")
        indices = np.asarray(aligned, dtype=int)
        fields = ("net_income_ttm", "net_assets", "roe", "pe", "pb")
        values = {
            field: np.asarray(fundamentals.column(field).to_pylist(), dtype=float)[indices]
            for field in fields
        }
        raw_high = _panel_array(prices.column("high"), unique_indices, len(calendar))
        raw_close = _panel_array(prices.column("close"), unique_indices, len(calendar))
        return cls(
            calendar=calendar,
            symbols=price_symbols,
            names=tuple(prices.column("name").to_pylist()[index] for index in unique_indices),
            open=_panel_array(prices.column("open"), unique_indices, len(calendar)),
            high=raw_high,
            low=_panel_array(prices.column("low"), unique_indices, len(calendar)),
            close=raw_close,
            signal_close=raw_close,
            signal_high=raw_high,
            volume=_panel_array(prices.column("volume"), unique_indices, len(calendar)),
            amount=_panel_array(prices.column("amount"), unique_indices, len(calendar)),
            industry=tuple(np.asarray(fundamentals.column("industry").to_pylist(), dtype=object)[indices]),
            fundamental_periods=tuple(fundamentals.column("period")[0].as_py()),
            fundamentals=values,
        )


@dataclass(frozen=True)
class BacktestResult:
    strategy_id: str
    equity: tuple[tuple[date, float], ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    targets: tuple[TargetPortfolio, ...]
    holdings: tuple[dict[str, Any], ...]
    signals: tuple[dict[str, Any], ...] = ()


def run_strategy(*args, **kwargs):
    raise RuntimeError("旧原价/简化账户运行接口已停用；请通过统一策略插件及历史输入检查运行。")


def _frame_at(
    market: ExploratoryMarket,
    index: int,
    held_since: dict[str, int],
    breadth_state: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None,
    include_industries: bool = False,
    build_candidates: bool = True,
) -> StrategyFrame:
    close = market.signal_close[:, index]
    held_days = {symbol: index - start for symbol, start in held_since.items()}
    active = _active_industries(market, index) if include_industries else set()
    if build_candidates:
        observed = valid_mask(
            *(values[:, : index + 1] for values in (
                market.open, market.high, market.low, market.close, market.volume,
                market.amount, market.signal_close, market.signal_high,
            ))
        )
        ma20 = np.full(len(market.symbols), np.nan)
        period_index = _available_period_index(market.fundamental_periods, market.calendar[index])
        fundamental = market.fundamentals
        candidates_list: list[Candidate] = []
        for symbol_index, symbol in enumerate(market.symbols):
            own_days = np.flatnonzero(observed[symbol_index]) if observed[symbol_index, index] else np.empty(0, dtype=int)
            ma120_value = amount20_value = amount120_value = r20_value = r120_value = drawdown20_value = np.nan
            if len(own_days) >= 20:
                recent20 = own_days[-20:]
                ma20[symbol_index] = float(np.mean(market.signal_close[symbol_index, recent20]))
                amount20_value = float(np.mean(market.amount[symbol_index, recent20]))
                drawdown20_value = float(close[symbol_index] / np.max(market.signal_high[symbol_index, recent20]) - 1)
            if len(own_days) >= 120:
                recent120 = own_days[-120:]
                ma120_value = float(np.mean(market.signal_close[symbol_index, recent120]))
                amount120_value = float(np.mean(market.amount[symbol_index, recent120]))
            if len(own_days) >= 21:
                r20_value = float(close[symbol_index] / market.signal_close[symbol_index, own_days[-21]] - 1)
            if len(own_days) >= 121:
                r120_value = float(close[symbol_index] / market.signal_close[symbol_index, own_days[-121]] - 1)
            candidates_list.append(Candidate(
                symbol=symbol,
                roe=_value(fundamental["roe"], symbol_index, period_index),
                pe=_value(fundamental["pe"], symbol_index, period_index),
                pb=_value(fundamental["pb"], symbol_index, period_index),
                amount20=_safe(amount20_value),
                amount120=_safe(amount120_value),
                close=_safe(close[symbol_index]) if len(own_days) else None,
                ma20=_safe(ma20[symbol_index]),
                ma120=_safe(ma120_value),
                r120=_safe(r120_value),
                drawdown20=_safe(drawdown20_value),
                industry=market.industry[symbol_index],
                net_income_ttm=_value(fundamental["net_income_ttm"], symbol_index, period_index),
                net_assets=_value(fundamental["net_assets"], symbol_index, period_index),
                r20=_safe(r20_value),
            ))
        candidates = tuple(candidates_list)
        breadth = float(np.nanmean(close > ma20))
    else:
        candidates = ()
        breadth = float("nan")
    if breadth_state is None:
        market_breadth = None
        market_breadth_smoothed = None
        market_on = False
        is_month_end = _is_month_end(market.calendar, index)
    else:
        raw, smoothed, on, month_end = breadth_state
        market_breadth = _safe(raw[index])
        market_breadth_smoothed = _safe(smoothed[index])
        market_on = bool(on[index])
        is_month_end = bool(month_end[index])
    return StrategyFrame(
        market.calendar[index],
        breadth,
        candidates,
        frozenset(active),
        held_days,
        market_breadth,
        market_breadth_smoothed,
        market_on,
        is_month_end,
    )


def _market_breadth_state(
    market: ExploratoryMarket,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute the clarified daily MA120 breadth gate without future data."""
    traded = valid_mask(
        market.open, market.high, market.low, market.close,
        market.volume, market.amount, market.signal_close, market.signal_high,
    )
    amount20 = own_day_rolling_mean(market.amount, 20, mask=traded)
    close_ma120 = own_day_rolling_mean(market.signal_close, 120, mask=traded)
    raw = np.full(len(market.calendar), np.nan, dtype=float)
    for index in range(len(market.calendar)):
        eligible = np.isfinite(amount20[:, index]) & (amount20[:, index] >= 50_000_000)
        denominator = int(eligible.sum())
        if denominator:
            numerator = int((eligible & np.isfinite(market.signal_close[:, index]) & (market.signal_close[:, index] > close_ma120[:, index])).sum())
            raw[index] = numerator / denominator
    smoothed = _rolling_nanmean_1d(raw, 5)
    market_on = np.zeros(len(raw), dtype=bool)
    for index in range(1, len(raw)):
        previous = market_on[index - 1]
        value = smoothed[index]
        if not np.isfinite(value):
            market_on[index] = previous
        elif previous:
            market_on[index] = value >= 0.45
        else:
            market_on[index] = value >= 0.60
    month_end = np.asarray(
        [
            index == len(market.calendar) - 1
            or (market.calendar[index + 1].year, market.calendar[index + 1].month)
            != (trade_date.year, trade_date.month)
            for index, trade_date in enumerate(market.calendar)
        ],
        dtype=bool,
    )
    return raw, smoothed, market_on, month_end


def _rolling_nanmean_1d(values: np.ndarray, window: int) -> np.ndarray:
    finite = np.isfinite(values)
    data = np.where(finite, values, 0.0)
    cumulative = np.cumsum(data, dtype=float)
    counts = np.cumsum(finite.astype(np.int32), dtype=np.int32)
    result = np.full(values.shape, np.nan, dtype=float)
    for index in range(window - 1, len(values)):
        start = index - window
        total = cumulative[index] - (cumulative[start] if start >= 0 else 0.0)
        count = counts[index] - (counts[start] if start >= 0 else 0)
        if count == window:
            result[index] = total / window
    return result


def _is_month_end(calendar: tuple[date, ...], index: int) -> bool:
    if index == len(calendar) - 1:
        return True
    current = calendar[index]
    following = calendar[index + 1]
    return (current.year, current.month) != (following.year, following.month)


def _orders_for_target(
    market: ExploratoryMarket,
    signal_index: int,
    execution_index: int,
    target: TargetPortfolio,
    book: PositionBook,
    equity: float,
    strategy_id: str,
    sequence_start: int,
) -> list[Order]:
    target_symbols = set(target.target_weights)
    symbols = sorted(set(book.holdings) | target_symbols, key=lambda symbol: (symbol in target_symbols, symbol))
    orders: list[Order] = []
    for offset, symbol in enumerate(symbols):
        symbol_index = market.symbols.index(symbol)
        signal_price = market.close[symbol_index, signal_index]
        if not np.isfinite(signal_price) or signal_price <= 0:
            continue
        target_weight = min(float(target.target_weights.get(symbol, 0.0)), 0.4)
        desired_shares = int((equity * target_weight / signal_price) // 100 * 100)
        existing = book.shares(symbol)
        delta = desired_shares - existing
        if delta == 0:
            continue
        amount20 = float(_row_nanmean(market.amount[symbol_index : symbol_index + 1, signal_index - 19 : signal_index + 1])[0])
        adv_cap = int((amount20 * 0.01 / signal_price) // 100 * 100) if np.isfinite(amount20) else 0
        requested = min(abs(delta), adv_cap)
        if requested <= 0:
            continue
        orders.append(
            Order(
                order_id=f"{strategy_id}-{target.as_of:%Y%m%d}-{sequence_start + offset:06d}",
                symbol=symbol,
                side=Side.BUY if delta > 0 else Side.SELL,
                requested_shares=requested,
                signal_date=target.as_of,
                submit_date=market.calendar[execution_index],
                reason=target.reasons.get(symbol, "target_exit"),
            )
        )
    return orders


def _bar(market: ExploratoryMarket, symbol_index: int, index: int) -> Bar:
    return Bar(
        market.symbols[symbol_index], market.calendar[index], *(
            _number(values[symbol_index, index]) for values in (market.open, market.high, market.low, market.close, market.volume, market.amount)
        )
    )


def _number(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _is_week_end(calendar: tuple[date, ...], index: int) -> bool:
    return index == len(calendar) - 1 or calendar[index].isocalendar()[:2] != calendar[index + 1].isocalendar()[:2]


def _available_period_index(periods: tuple[str, ...], as_of: date) -> int | None:
    eligible = [index for index, period in enumerate(periods) if conservative_available_date(period) <= as_of]
    return eligible[-1] if eligible else None


def _value(values: np.ndarray, row: int, period: int | None) -> float | None:
    if period is None:
        return None
    return _safe(values[row, period])


def _safe(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def _panel_array(column: Any, indices: np.ndarray, width: int) -> np.ndarray:
    array = column.combine_chunks()
    offsets = np.asarray(array.offsets.to_numpy(), dtype=np.int64)
    values = np.asarray(array.values.to_numpy(zero_copy_only=False), dtype=float)
    output = np.full((len(offsets) - 1, width), np.nan, dtype=float)
    for row_index in range(len(offsets) - 1):
        start, end = int(offsets[row_index]), int(offsets[row_index + 1])
        if end > start:
            output[row_index, : min(end - start, width)] = values[start : start + min(end - start, width)]
    return output[indices]


def _row_nanmean(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    counts = finite.sum(axis=1)
    output = np.full(values.shape[0], np.nan)
    np.divide(np.where(finite, values, 0.0).sum(axis=1), counts, out=output, where=counts > 0)
    return output


def _row_nanmax(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    output = np.where(finite, values, -np.inf).max(axis=1)
    output[~finite.any(axis=1)] = np.nan
    return output


def _industry_returns(industries: tuple[str | None, ...], returns: np.ndarray) -> dict[str, float]:
    output: dict[str, list[float]] = {}
    for industry, value in zip(industries, returns, strict=True):
        if industry and np.isfinite(value):
            output.setdefault(industry, []).append(float(value))
    return {industry: float(np.mean(values)) for industry, values in output.items()}


def _active_industries(market: ExploratoryMarket, index: int) -> set[str]:
    """Use only completed weekly observations and require 3/4 consecutive weeks."""
    weekly_ends: list[int] = []
    for candidate in range(index, 59, -1):
        if _is_week_end(market.calendar, candidate):
            weekly_ends.append(candidate)
            if len(weekly_ends) == 4:
                break
    weekly_ends.reverse()
    if len(weekly_ends) < 3:
        return set()
    qualifying: list[set[str]] = []
    for week_index in weekly_ends[-4:]:
        returns = market.close[:, week_index] / market.close[:, week_index - 60] - 1
        by_industry = _industry_returns(market.industry, returns)
        cutoff = max(1, int(np.ceil(len(by_industry) * 0.2)))
        ranked = sorted(by_industry.items(), key=lambda item: (-item[1], item[0]))
        qualifying.append({industry for industry, value in ranked[:cutoff] if value >= 0.15})
    active = set.intersection(*qualifying[-3:])
    if len(qualifying) >= 4:
        active |= set.intersection(*qualifying[-4:])
    return active
