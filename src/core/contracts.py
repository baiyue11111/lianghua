"""Immutable contracts shared by every kernel plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Mapping, TypeAlias


Scalar: TypeAlias = str | int | float | bool | None


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _name(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")


def _floats(values: Mapping[str, float], name: str) -> Mapping[str, float]:
    copied = dict(values)
    for key, value in copied.items():
        _name(key, f"{name} key")
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(f"{name} values must be finite numbers")
    return MappingProxyType(copied)


def _decimals(values: Mapping[str, Decimal], name: str) -> Mapping[str, Decimal]:
    copied = dict(values)
    for key, value in copied.items():
        _name(key, f"{name} key")
        _decimal(value, f"{name} value")
    return MappingProxyType(copied)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class DataRequest:
    fields: tuple[str, ...]
    history_bars: int
    frequency: str = "daily"
    price_basis: str = "adjusted"
    symbols: tuple[str, ...] | None = None
    universe: str = "dataset"

    def __post_init__(self) -> None:
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("unique required data fields are required")
        for name in self.fields:
            _name(name, "data field")
        if type(self.history_bars) is not int or self.history_bars < 1:
            raise ValueError("history_bars must be a positive integer")
        if self.symbols is not None:
            if not self.symbols or tuple(sorted(set(self.symbols))) != self.symbols:
                raise ValueError("symbols must be an ordered unique nonempty tuple")
            for symbol in self.symbols: _name(symbol,"requested symbol")
        _name(self.universe,"data universe")
        _name(self.frequency, "data frequency")
        _name(self.price_basis, "price basis")


@dataclass(frozen=True)
class HistoryBar:
    at: datetime
    fields: Mapping[str, Scalar]

    def __post_init__(self) -> None:
        _aware(self.at, "history timestamp")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class MarketFrame:
    at: datetime
    venue: str
    fields: Mapping[str, Mapping[str, Scalar]]
    context: Mapping[str, Scalar] = field(default_factory=dict)
    history: Mapping[str, tuple[HistoryBar, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.at, "market timestamp")
        _name(self.venue, "venue")
        copied = {}
        for instrument, values in self.fields.items():
            _name(instrument, "instrument")
            if not isinstance(values, Mapping):
                raise TypeError("instrument fields must be a mapping")
            copied[instrument] = MappingProxyType(dict(values))
        object.__setattr__(self, "fields", MappingProxyType(copied))
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))
        history = {}
        for instrument, rows in self.history.items():
            _name(instrument, "history instrument")
            rows = tuple(rows)
            if any(not isinstance(row, HistoryBar) for row in rows):
                raise TypeError("history rows must be HistoryBar")
            times = tuple(row.at for row in rows)
            if tuple(sorted(set(times))) != times or any(at > self.at for at in times):
                raise ValueError("history must be ordered, unique and not future")
            history[instrument] = rows
        object.__setattr__(self, "history", MappingProxyType(history))


@dataclass(frozen=True)
class TargetSet:
    source_id: str
    as_of: datetime
    weights: Mapping[str, float]
    reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.source_id, "source_id")
        _aware(self.as_of, "target timestamp")
        object.__setattr__(self, "weights", _floats(self.weights, "weights"))
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))


@dataclass(frozen=True)
class OrderRequest:
    order_id: str
    created_at: datetime
    instrument: str
    side: Side
    quantity: Decimal

    def __post_init__(self) -> None:
        _name(self.order_id, "order_id")
        _aware(self.created_at, "order timestamp")
        _name(self.instrument, "instrument")
        if not isinstance(self.side, Side):
            raise TypeError("side must be Side")
        _decimal(self.quantity, "quantity", positive=True)


@dataclass(frozen=True)
class FillEvent:
    order_id: str
    at: datetime
    instrument: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_components: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.order_id, "order_id")
        _aware(self.at, "fill timestamp")
        _name(self.instrument, "instrument")
        if not isinstance(self.side, Side):
            raise TypeError("side must be Side")
        _decimal(self.quantity, "quantity", positive=True)
        _decimal(self.price, "price", positive=True)
        _decimal(self.fee, "fee")
        if self.fee < 0:
            raise ValueError("fee must be non-negative")
        components = _decimals(self.fee_components, "fee_components")
        if any(value < 0 for value in components.values()):
            raise ValueError("fee components must be non-negative")
        object.__setattr__(self, "fee_components", components)


@dataclass(frozen=True)
class OrderRejection:
    order_id: str
    at: datetime
    reason: str

    def __post_init__(self) -> None:
        _name(self.order_id, "order_id")
        _aware(self.at, "rejection timestamp")
        _name(self.reason, "rejection reason")


@dataclass(frozen=True)
class ExecutionResult:
    as_of: datetime
    orders: tuple[OrderRequest, ...]
    fills: tuple[FillEvent, ...]
    remaining: tuple[OrderRequest, ...] = ()
    rejections: tuple[OrderRejection, ...] = ()

    def __post_init__(self) -> None:
        _aware(self.as_of, "execution timestamp")
        object.__setattr__(self, "orders", tuple(self.orders))
        object.__setattr__(self, "fills", tuple(self.fills))
        object.__setattr__(self, "remaining", tuple(self.remaining))
        object.__setattr__(self, "rejections", tuple(self.rejections))


@dataclass(frozen=True)
class AccountSnapshot:
    as_of: datetime
    cash: Decimal
    equity: Decimal
    positions: Mapping[str, Decimal]
    sellable: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.as_of, "account timestamp")
        _decimal(self.cash, "cash")
        _decimal(self.equity, "equity")
        object.__setattr__(self, "positions", _decimals(self.positions, "positions"))
        object.__setattr__(self, "sellable", _decimals(self.sellable, "sellable"))


@dataclass(frozen=True)
class StepResult:
    frame: MarketFrame
    execution: ExecutionResult
    account: AccountSnapshot
    decision: TargetSet | None
    target: TargetSet | None
    planned_orders: tuple[OrderRequest, ...]
