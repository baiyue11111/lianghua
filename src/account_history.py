"""Chronological orchestration, without source loading or performance verdicts.

Inputs must come from source/feature/tax validation upstream. A successful
trace is NOT a certificate of full-universe, adjustment or tax completeness.
The declared account policy and actual fills determine taxes and liabilities.
No precomputed parameter-bound account results are accepted.
"""
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import re
from typing import Iterable, Mapping, Protocol

from account_allocation import AllocationLimits, AllocationPlan
from account_execution import AccountExecutionSession
from account_ledger import AccountLedger, AccountPoint, CashPosting, OrderCancellation, PositionLot
from contracts import Bar, Fill, Order, TargetPortfolio
from core.contracts import AccountSnapshot, MarketFrame, TargetSet
from core.interfaces import Strategy
from corporate_actions import CorporateActionEvent, DistributionEntitlement, calculate_entitlement
from distribution_ledger import AccountDistributionLedger, DistributionPosting, ShareDelivery
from execution_risk import OpeningRiskSnapshot, nonnegative
from tax_ledger import TaxLedger


@dataclass(frozen=True)
class RegistrationAction:
    distribution_id: str
    event: CorporateActionEvent
    source_sha256: str
    share_credit_date: date | None
    stock_nominal: Decimal
    reserve_taxable: Decimal
    tax_source_sha256: str


@dataclass(frozen=True)
class HistoryDay:
    trade_date: date
    frame: MarketFrame
    bars: Mapping[str,Bar]
    previous_closes: Mapping[str,float | None]
    opening_marks: Mapping[str,Decimal]
    closing_marks: Mapping[str,Decimal]
    opening_amount20: Mapping[str,Decimal]
    closing_amount20: Mapping[str,Decimal]
    liquidity_as_of: date
    registrations: tuple[RegistrationAction,...]


@dataclass(frozen=True)
class RegistrationRecord:
    action: RegistrationAction
    entitlement: DistributionEntitlement
    deliveries: tuple[ShareDelivery,...]


@dataclass(frozen=True)
class HistoryResult:
    frames: tuple[MarketFrame,...]
    targets: tuple[TargetSet | None,...]
    allocations: tuple[AllocationPlan,...]
    orders: tuple[Order,...]
    fills: tuple[Fill,...]
    pending: tuple[Order,...]
    cancellations: tuple[OrderCancellation,...]
    cash_events: tuple[CashPosting,...]
    equity: tuple[AccountPoint,...]
    holdings: tuple[tuple[date,tuple[PositionLot,...]],...]
    distribution_postings: tuple[DistributionPosting,...]
    registrations: tuple[RegistrationRecord,...]
    tax_payments: tuple
    tax_liabilities: tuple
    tax_pending: tuple
    tax_holdings: tuple


class HistoryRunError(ValueError):
    """Failed-run evidence, never a successful result or performance verdict."""
    def __init__(self, cause, *, failed_day, partial_trace, cash, lots):
        super().__init__(f'history failed on {failed_day}: {type(cause).__name__}: {cause}')
        self.failed_day=failed_day; self.partial_trace=partial_trace
        self.cash=cash; self.lots=lots


def _validate_day(record, day):
    if record.trade_date!=day or record.frame.at.date()!=day:
        raise ValueError('history day/frame do not match required calendar session')
    if record.liquidity_as_of>=day:
        raise ValueError('opening liquidity cannot use current/future session')
    for action in record.registrations:
        event=action.event
        if (not action.distribution_id or not event.executable or event.registration_date!=day
                or not re.fullmatch('[0-9a-f]{64}',action.source_sha256)):
            raise ValueError('identified executable registration action required')
        if event.stock_dividend_per_share+event.reserve_to_stock_per_share>0:
            if (not isinstance(action.share_credit_date,date)
                    or not event.operation_date<=action.share_credit_date<=event.stock_market_date):
                raise ValueError('explicit share credit date required; listing date is not a substitute')


def _register(action, account, distributions):
    lots=tuple(lot for lot in account.lots if lot.symbol==action.event.code)
    claim=calculate_entitlement(action.event,action.event.registration_date,sum(l.shares for l in lots))
    deliveries=[]
    ratio=Decimal(str(action.event.stock_dividend_per_share))+Decimal(str(action.event.reserve_to_stock_per_share))
    for lot in lots:
        shares=Decimal(lot.shares)*ratio
        if shares!=shares.to_integral_value():
            raise ValueError('fractional lot allocation requires source evidence; no rounding')
        if shares:
            deliveries.append(ShareDelivery(lot.lot_id,lot.acquired,int(shares),
                                            action.share_credit_date,action.event.stock_market_date))
    distributions.register(action.distribution_id,claim,action.source_sha256,tuple(deliveries))
    return RegistrationRecord(action,claim,tuple(deliveries))


def run_account_history(strategy: Strategy, *, calendar: tuple[date,...],
                        days: Iterable[HistoryDay], initial_cash: Decimal, fees, tax_policy,
                        limits: AllocationLimits=AllocationLimits(), trade_start: date | None = None) -> HistoryResult:
    """Calendar contains every evaluated session plus one next-session sentinel.

    Stream days chronologically. Never force liquidation or infer month end
    from the truncated data window. Failed/incomplete input raises; it does
    not return a successful trace or publish performance artifacts.
    """
    if len(calendar)<2 or any(not isinstance(day,date) for day in calendar) or tuple(sorted(set(calendar)))!=calendar:
        raise ValueError('ordered unique session calendar and next-session sentinel required')
    from plugins.data import Settlement

    settlement = Settlement(initial_cash, fees, tax_policy, limits)
    count=0
    try:
        for index,record in enumerate(days):
            if index>=len(calendar)-1: raise ValueError('history exceeds declared calendar coverage')
            day=calendar[index]; following=calendar[index+1]
            _validate_day(record, day)
            frame, snapshot = settlement.apply(record, following, trade_start)
            target = strategy.generate(frame, snapshot)
            if target is not None:
                if not isinstance(target,TargetSet) or target.as_of!=frame.at or target.source_id!=strategy.strategy_id:
                    raise ValueError('strategy target identity/time do not match decision')
            settlement.plan(target, strategy.strategy_id)
            count += 1
        if count!=len(calendar)-1: raise ValueError('history is missing required calendar sessions')
    except Exception as error:
        settlement.fail(error)
        raise HistoryRunError(error,failed_day=calendar[min(count,len(calendar)-1)],
                              partial_trace=settlement.result(),cash=settlement.cash,lots=settlement.lots) from error
    return settlement.result()
