"""Pure execution-risk contract; no strategy, source or account mutation."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping

from account_allocation import AllocationLimits
from contracts import Fill, Order, Side


@dataclass(frozen=True)
class OpeningRiskSnapshot:
    liquidity_as_of: date
    raw_marks: Mapping[str,Decimal]
    amount20: Mapping[str,Decimal]
    receivables: Decimal
    liabilities: Decimal
    limits: AllocationLimits = AllocationLimits()


def nonnegative(value):
    value=Decimal(str(value))
    if not value.is_finite() or value<0:
        raise ValueError('finite nonnegative opening risk input required')
    return value


def validate_opening_risk(snapshot, trade_date, holdings, orders):
    if not isinstance(snapshot.liquidity_as_of,date) or snapshot.liquidity_as_of>=trade_date:
        raise ValueError('liquidity must be observed before execution day')
    for symbol in set(holdings)|{order.symbol for order in orders}:
        if symbol not in snapshot.raw_marks:
            raise ValueError(f'missing opening mark: {symbol}')
        nonnegative(snapshot.raw_marks[symbol])
    for order in orders:
        if order.symbol not in snapshot.amount20:
            raise ValueError(f'missing observed liquidity: {order.symbol}')
        nonnegative(snapshot.amount20[order.symbol])
    for value in (snapshot.receivables,snapshot.liabilities): nonnegative(value)
    for value in (snapshot.limits.stock_weight,snapshot.limits.adv_fraction):
        if not 0<nonnegative(value)<=1:
            raise ValueError('opening risk fractions must be in (0,1]')


def opening_equity(cash,holdings,snapshot):
    return (cash+sum((nonnegative(snapshot.raw_marks[s])*n for s,n in holdings.items()),Decimal(0))
            +nonnegative(snapshot.receivables)-nonnegative(snapshot.liabilities))


def opening_rejection(order: Order, fill: Fill, *, equity: Decimal,
                      holdings: Mapping[str,int], snapshot: OpeningRiskSnapshot):
    if fill.status!='filled': return None
    if (Decimal(str(fill.price))*order.requested_shares>
            nonnegative(snapshot.amount20[order.symbol])*nonnegative(snapshot.limits.adv_fraction)):
        return 'order_above_adv_limit'
    if order.side is Side.BUY:
        mark=nonnegative(snapshot.raw_marks[order.symbol])
        post_equity=(equity-Decimal(str(fill.gross_amount))-Decimal(str(fill.commission))
                     -Decimal(str(fill.stamp_duty))+fill.shares*mark)
        post_value=(holdings.get(order.symbol,0)+fill.shares)*mark
        if post_equity<=0 or post_value>post_equity*nonnegative(snapshot.limits.stock_weight):
            return 'position_above_stock_limit'
    return None
