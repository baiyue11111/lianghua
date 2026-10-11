"""Pure domain contracts shared by data, strategy, execution, and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Mapping


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Bar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    @classmethod
    def open_bar(cls, symbol: str, trade_date: date, price: float, volume: float, amount: float) -> "Bar":
        return cls(symbol, trade_date, price, price, price, price, volume, amount)


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: Side
    requested_shares: int
    signal_date: date
    submit_date: date
    reason: str


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    trade_date: date
    shares: int
    price: float
    gross_amount: float
    commission: float
    stamp_duty: float
    status: str
    reason: str | None = None
    transfer_fee: float = 0.0


@dataclass(frozen=True)
class Candidate:
    """Cross-sectional features available at one signal timestamp."""

    symbol: str
    roe: float | None
    pe: float | None
    pb: float | None
    amount20: float | None
    close: float | None
    ma120: float | None
    r120: float | None
    drawdown20: float | None = None
    ma20: float | None = None
    amount120: float | None = None
    industry: str | None = None
    net_income_ttm: float | None = None
    net_assets: float | None = None
    r20: float | None = None


@dataclass(frozen=True)
class StrategyFrame:
    as_of: date
    breadth_exposure: float
    candidates: tuple[Candidate, ...]
    active_industries: frozenset[str] = frozenset()
    held_days: Mapping[str, int] | None = None
    # Optional shared features for strategies whose market gate is explicitly
    # defined by amount-filtered MA120 breadth rather than the legacy exposure
    # scalar used by the original three strategies.
    market_breadth_120: float | None = None
    market_breadth_120_smoothed: float | None = None
    market_on: bool = False
    is_month_end: bool = False


@dataclass(frozen=True)
class TargetPortfolio:
    strategy_id: str
    as_of: date
    target_weights: Mapping[str, float]
    gross_exposure: float
    reasons: Mapping[str, str]
    # An observation-only signal must not resize holdings or replace pending
    # orders. This is an execution instruction, not an inferred weight change.
    rebalance: bool = True
