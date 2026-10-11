"""The only stateful execution component for all strategies."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import floor, isfinite
from typing import Mapping

from contracts import Bar, Fill, Order, Side
from fees import FeePolicy


BOARD_LOT = 100
PRICE_TICK = Decimal("0.01")
SLIPPAGE = Decimal("0.001")


def _execution_price(open_price: float, side: Side) -> float:
    """Return an adverse-slippage estimate on a valid A-share price tick."""
    direction = Decimal(1) + SLIPPAGE if side is Side.BUY else Decimal(1) - SLIPPAGE
    raw_price = Decimal(str(open_price)) * direction
    rounding = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
    ticks = (raw_price / PRICE_TICK).to_integral_value(rounding=rounding)
    return float(max(PRICE_TICK, ticks * PRICE_TICK))


def remaining_order(order: Order, fill: Fill) -> Order | None:
    """Retain unfilled quantity with the original signal and order identity."""
    if (fill.order_id, fill.symbol, fill.side) != (order.order_id, order.symbol, order.side):
        raise ValueError('fill does not belong to order')
    if fill.shares < 0 or fill.shares > order.requested_shares:
        raise ValueError('fill quantity outside order bounds')
    remaining = order.requested_shares - fill.shares
    return replace(order, requested_shares=remaining) if remaining else None


@dataclass
class PositionBook:
    cash: float
    holdings: dict[str, int] = field(default_factory=dict)
    average_costs: dict[str, float] = field(default_factory=dict)
    purchases: dict[str, tuple[date, int]] = field(default_factory=dict)

    def __init__(
        self,
        cash: float,
        holdings: Mapping[str, int] | None = None,
        average_costs: Mapping[str, float] | None = None,
    ) -> None:
        self.cash = float(cash)
        self.holdings = dict(holdings or {})
        self.average_costs = dict(average_costs or {})
        # Constructor holdings are settled opening balances. New fills are
        # tracked separately so a prior-day signal cannot bypass share T+1.
        self.purchases = {}

    def shares(self, symbol: str) -> int:
        return self.holdings.get(symbol, 0)

    def sellable_shares(self, symbol: str, trade_date: date) -> int:
        purchase = self.purchases.get(symbol)
        locked = purchase[1] if purchase and purchase[0] >= trade_date else 0
        return max(0, self.shares(symbol) - locked)


class ExecutionEngine:
    """Next-open, adverse-slippage, board-lot A-share execution."""

    def __init__(self, fees: FeePolicy):
        self.fees = fees

    def execute(self, order: Order, bar: Bar, book: PositionBook, previous_close: float | None = None) -> Fill:
        rejection = self._rejection_reason(order, bar, previous_close)
        if rejection:
            return Fill(order.order_id, order.symbol, order.side, bar.trade_date, 0, 0, 0, 0, 0, "rejected", rejection)

        executable = self._rounded_shares(order, bar, book)
        if executable == 0:
            return Fill(order.order_id, order.symbol, order.side, bar.trade_date, 0, 0, 0, 0, 0, "rejected", "insufficient_cash_or_shares")

        price = _execution_price(bar.open, order.side)
        gross = round(price * executable, 6)
        commission, stamp_duty, transfer = map(float, self.fees.calculate(Decimal(str(gross)),order.side,bar.trade_date))

        if order.side is Side.BUY:
            current = book.shares(order.symbol)
            old_cost = book.average_costs.get(order.symbol, 0.0) * current
            total_shares = current + executable
            if total_shares <= 0:
                return Fill(order.order_id, order.symbol, order.side, bar.trade_date, 0, 0, 0, 0, 0, "rejected", "invalid_position_after_buy")
            book.cash -= gross + commission + transfer
            book.holdings[order.symbol] = total_shares
            book.average_costs[order.symbol] = (old_cost + gross + commission + transfer) / total_shares
            prior = book.purchases.get(order.symbol)
            same_day = prior[1] if prior and prior[0] == bar.trade_date else 0
            book.purchases[order.symbol] = (bar.trade_date, same_day + executable)
        else:
            book.cash += gross - commission - stamp_duty - transfer
            remaining = book.shares(order.symbol) - executable
            if remaining:
                book.holdings[order.symbol] = remaining
            else:
                book.holdings.pop(order.symbol, None)
                book.average_costs.pop(order.symbol, None)

        return Fill(order.order_id, order.symbol, order.side, bar.trade_date, executable, price, gross, commission, stamp_duty, "filled", transfer_fee=transfer)

    @staticmethod
    def _rejection_reason(order: Order, bar: Bar, previous_close: float | None) -> str | None:
        if order.symbol != bar.symbol:
            return "symbol_mismatch"
        if not isfinite(order.requested_shares) or order.requested_shares <= 0:
            return "invalid_order_quantity"
        if bar.trade_date <= order.signal_date:
            return "trade_before_signal_date"
        if bar.trade_date < order.submit_date:
            return "trade_before_submit_date"
        if not isfinite(bar.open) or bar.open <= 0:
            return "invalid_price"
        if not isfinite(bar.volume) or bar.volume <= 0:
            return "suspended_or_zero_volume"
        if previous_close and bar.open == bar.high == bar.low:
            if order.side is Side.BUY and bar.open > previous_close:
                return "one_price_limit_up"
            if order.side is Side.SELL and bar.open < previous_close:
                return "one_price_limit_down"
        return None

    def _rounded_shares(self,order: Order, bar: Bar, book: PositionBook) -> int:
        requested = floor(order.requested_shares / BOARD_LOT) * BOARD_LOT
        if order.side is Side.SELL:
            held = book.sellable_shares(order.symbol, bar.trade_date)
            limit = max(0, min(order.requested_shares, held))
            whole = floor(limit / BOARD_LOT) * BOARD_LOT
            odd = held % BOARD_LOT
            # Odd balance may be sold together, never split into smaller pieces.
            with_odd = floor((limit - odd) / BOARD_LOT) * BOARD_LOT + odd if limit >= odd else 0
            return max(whole, with_odd)

        adverse_price = _execution_price(bar.open, Side.BUY)
        affordable = max(0, floor(book.cash / adverse_price / BOARD_LOT) * BOARD_LOT)
        while affordable:
            gross = Decimal(str(adverse_price)) * affordable
            fees = sum(self.fees.calculate(gross,Side.BUY,bar.trade_date))
            if gross + fees <= Decimal(str(book.cash)): break
            affordable -= BOARD_LOT
        return min(requested, affordable)
