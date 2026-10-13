"""Pure allocation from explicit close snapshots, never signal-adjusted prices.

This module neither reads sources nor mutates an account. Limits apply to the
signal-close sizing amount; next-open price gaps still require execution-time
risk validation by the caller. It does not certify a backtest for release.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Mapping

from protocal import Order, Side, TargetSet


@dataclass(frozen=True)
class AllocationLimits:
    lot_size: int = 100
    stock_weight: Decimal = Decimal('0.4')
    adv_fraction: Decimal = Decimal('0.01')


@dataclass(frozen=True)
class AllocationDecision:
    symbol: str
    target_shares: int
    delta: int
    status: str
    reason: str


@dataclass(frozen=True)
class AllocationPlan:
    orders: tuple[Order, ...]
    decisions: tuple[AllocationDecision, ...]


def _positive(value):
    value=Decimal(str(value))
    if not value.is_finite() or value<=0:
        raise ValueError('positive finite sizing observation required')
    return value


def allocate_target(target: TargetSet, *, next_session: date,
                    equity: Decimal, holdings: Mapping[str,int],
                    raw_closes: Mapping[str,Decimal], amount20: Mapping[str,Decimal],
                    limits: AllocationLimits=AllocationLimits(), rebalance: bool = True) -> AllocationPlan:
    if next_session <= target.as_of.date():
        raise ValueError('next-session execution date required')
    if not rebalance:
        return AllocationPlan((),())
    equity=_positive(equity)
    if type(limits.lot_size) is not int or limits.lot_size<=0:
        raise ValueError('positive integer lot size required')
    weight_cap=_positive(limits.stock_weight); adv_cap=_positive(limits.adv_fraction)
    if max(weight_cap,adv_cap)>1:
        raise ValueError('risk fractions cannot exceed one')
    weights={symbol:Decimal(str(weight)) for symbol,weight in target.weights.items()}
    exposure=sum(weights.values(), Decimal(0))
    if (not exposure.is_finite() or not 0<=exposure<=1 or
            any(not w.is_finite() or w<0 for w in weights.values()) or
            sum(weights.values(),Decimal(0))>1 or
            abs(sum(weights.values(),Decimal(0))-exposure)>Decimal('1e-12')):
        raise ValueError('finite unleveraged consistent target weights required')
    if any(type(n) is not int or n<0 for n in holdings.values()):
        raise ValueError('integral nonnegative holdings required')
    symbols=sorted(set(holdings)|set(weights))
    observations={}
    for symbol in symbols:
        if symbol not in raw_closes or symbol not in amount20:
            raise ValueError(f'missing allocation observation: {symbol}')
        observations[symbol]=(_positive(raw_closes[symbol]),_positive(amount20[symbol]))
    orders=[]; decisions=[]
    for symbol in symbols:
        price,adv=observations[symbol]; weight=weights.get(symbol,Decimal(0))
        desired=int(equity*weight/price//limits.lot_size)*limits.lot_size
        existing=holdings.get(symbol,0); delta=desired-existing
        requested=delta//limits.lot_size*limits.lot_size if delta>0 else abs(delta)
        reason=None
        if weight>weight_cap: reason='weight_above_stock_limit'
        elif (weight>0 and desired==0 and existing==0) or (delta>0 and requested==0): reason='below_one_lot'
        elif requested*price>adv*adv_cap: reason='order_above_adv_limit'
        if reason:
            decisions.append(AllocationDecision(symbol,desired,delta,'skipped',reason))
        elif delta==0:
            decisions.append(AllocationDecision(symbol,desired,delta,'unchanged','already_at_target'))
        else:
            side=Side.BUY if delta>0 else Side.SELL
            reason=target.reasons.get(symbol,'target_exit')
            identity=repr((target.source_id,target.as_of.date(),next_session,symbol,side.value,requested,reason))
            orders.append(Order(sha256(identity.encode()).hexdigest(),symbol,side,requested,
                                target.as_of.date(),next_session,reason))
            decisions.append(AllocationDecision(symbol,desired,delta,'ordered',reason))
    orders.sort(key=lambda order:(order.side!=Side.SELL,order.symbol))
    return AllocationPlan(tuple(orders),tuple(decisions))
