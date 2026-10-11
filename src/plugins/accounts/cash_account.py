"""Long-only cash account with explicit T+1 sellability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.contracts import AccountSnapshot, ExecutionResult, MarketFrame, Side


@dataclass
class _Lot:
    acquired_at: datetime
    quantity: Decimal


class CashAccount:
    def __init__(self, *, as_of: datetime, initial_cash: Decimal) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(initial_cash, Decimal) or not initial_cash.is_finite() or initial_cash < 0:
            raise ValueError("initial_cash must be a finite non-negative Decimal")
        self._as_of = as_of
        self._cash = initial_cash
        self._equity = initial_cash
        self._lots: dict[str, list[_Lot]] = {}

    def snapshot(self) -> AccountSnapshot:
        positions = {
            symbol: sum((lot.quantity for lot in lots), Decimal("0"))
            for symbol, lots in sorted(self._lots.items())
            if lots
        }
        sellable = {
            symbol: sum(
                (lot.quantity for lot in lots if lot.acquired_at < self._as_of),
                Decimal("0"),
            )
            for symbol, lots in sorted(self._lots.items())
            if any(lot.acquired_at < self._as_of for lot in lots)
        }
        return AccountSnapshot(self._as_of, self._cash, self._equity, positions, sellable)

    def apply(self, result: ExecutionResult, frame: MarketFrame) -> AccountSnapshot:
        if result.as_of != frame.at:
            raise ValueError("execution and market timestamps differ")
        if frame.at <= self._as_of:
            raise ValueError("account timestamps must be strictly increasing")

        for fill in result.fills:
            gross = fill.price * fill.quantity
            if fill.side is Side.BUY:
                cost = gross + fill.fee
                if cost > self._cash:
                    raise ValueError("buy fill exceeds available cash")
                self._cash -= cost
                self._lots.setdefault(fill.instrument, []).append(_Lot(fill.at, fill.quantity))
            else:
                self._sell(fill.instrument, fill.quantity, fill.at)
                self._cash += gross - fill.fee

        market_value = Decimal("0")
        for symbol, lots in self._lots.items():
            if not lots:
                continue
            values = frame.fields.get(symbol)
            if values is None or "close" not in values:
                raise KeyError(f"missing close for held instrument {symbol}")
            price = Decimal(str(values["close"]))
            if not price.is_finite() or price <= 0:
                raise ValueError(f"invalid close for held instrument {symbol}")
            quantity = sum((lot.quantity for lot in lots), Decimal("0"))
            market_value += quantity * price

        self._as_of = frame.at
        self._equity = self._cash + market_value
        return self.snapshot()

    def _sell(self, symbol: str, quantity: Decimal, at: datetime) -> None:
        lots = self._lots.get(symbol, [])
        available = sum((lot.quantity for lot in lots if lot.acquired_at < at), Decimal("0"))
        if quantity > available:
            raise ValueError("sell fill exceeds T+1 sellable quantity")
        remaining = quantity
        kept: list[_Lot] = []
        for lot in lots:
            if remaining > 0 and lot.acquired_at < at:
                used = min(lot.quantity, remaining)
                lot.quantity -= used
                remaining -= used
            if lot.quantity > 0:
                kept.append(lot)
        if kept:
            self._lots[symbol] = kept
        else:
            self._lots.pop(symbol, None)
