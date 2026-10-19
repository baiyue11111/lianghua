"""Shared business calculations for allocation, execution, settlement and features."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from hashlib import sha256
import math
from math import floor, isfinite, sqrt
from numbers import Integral
import re
from statistics import fmean, stdev

import numpy as np

from protocol import Bar, Fill, Order, Side, TargetSet, Trial, validate_execution
from strategy import canonical_hash


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
    def select(candidates, positions, score, descending=True, symbol=lambda row: row[-1]):
        """Sort strategy candidates by a declared score and take the first N."""
        ordered = sorted(candidates, key=symbol)
        ordered = sorted(ordered, key=score, reverse=descending)
        return tuple(symbol(row) for row in ordered[:positions])

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
        periods: int = 252, risk_free_rate: int | float = 0.0,
    ) -> dict[str, float | int | None]:
        points = tuple(points)
        if not points:
            raise ValueError("equity points are required")
        if type(periods) is not int or periods < 1:
            raise ValueError("periods must be a positive integer")
        if type(risk_free_rate) not in (int, float):
            raise ValueError("risk_free_rate must be a finite annual number")
        Calculation._finite(risk_free_rate, "risk_free_rate")
        if risk_free_rate <= -1:
            raise ValueError("risk_free_rate must be greater than -1")
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
                period_rate = (1 + risk_free_rate) ** (1 / periods) - 1
                excess = [value - period_rate for value in returns]
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

    @staticmethod
    def valid_mask(*arrays: np.ndarray, positive: bool = True) -> np.ndarray:
        """Return the element-wise mask of finite, tradable observations."""
        if not arrays:
            raise ValueError("valid_mask needs at least one array")
        mask = np.ones(arrays[0].shape, dtype=bool)
        for array in arrays:
            mask &= np.isfinite(array)
            if positive:
                mask &= array > 0
        return mask

    @staticmethod
    def own_day_rolling_mean(
        values: np.ndarray, window: int, mask: np.ndarray | None = None
    ) -> np.ndarray:
        """Compute a rolling mean over each row's own valid observations."""
        if window < 1:
            raise ValueError("window must be >= 1")
        values = np.asarray(values, dtype=np.float64)
        if mask is None:
            mask = np.isfinite(values)
        out = np.full(values.shape, np.nan, dtype=np.float64)
        for row in range(values.shape[0]):
            valid = mask[row]
            count = int(valid.sum())
            if count < window:
                continue
            observed = values[row][valid]
            if not np.isfinite(observed).all():
                raise ValueError(f"row {row}: mask admits a non-finite value")
            cumulative = np.empty(count + 1, dtype=np.float64)
            cumulative[0] = 0.0
            np.cumsum(observed, out=cumulative[1:])
            rolling = np.full(count, np.nan, dtype=np.float64)
            rolling[window - 1:] = (
                cumulative[window:] - cumulative[:-window]
            ) / window
            out[row][valid] = rolling
        return out

    @staticmethod
    def own_day_lagged_return(
        values: np.ndarray, window: int, mask: np.ndarray | None = None
    ) -> np.ndarray:
        """Compute returns by each row's own trading-day count."""
        if window < 1:
            raise ValueError("window must be >= 1")
        values = np.asarray(values, dtype=np.float64)
        if mask is None:
            mask = np.isfinite(values) & (values > 0)
        out = np.full(values.shape, np.nan, dtype=np.float64)
        for row in range(values.shape[0]):
            valid = mask[row]
            count = int(valid.sum())
            if count <= window:
                continue
            observed = values[row][valid]
            rolling = np.full(count, np.nan, dtype=np.float64)
            rolling[window:] = observed[window:] / observed[:-window] - 1.0
            out[row][valid] = rolling
        return out

    @staticmethod
    def own_day_count(mask: np.ndarray) -> np.ndarray:
        """Count valid observations cumulatively for each row."""
        return np.cumsum(mask.astype(np.int32), axis=1)

    @staticmethod
    def trailing_mean(series: np.ndarray, window: int) -> np.ndarray:
        """Compute a dense one-dimensional trailing mean."""
        if window < 1:
            raise ValueError("window must be >= 1")
        series = np.asarray(series, dtype=np.float64)
        out = np.full(series.shape, np.nan, dtype=np.float64)
        for index in range(window - 1, series.shape[0]):
            values = series[index - window + 1:index + 1]
            if np.isfinite(values).all():
                out[index] = values.mean()
        return out

    @staticmethod
    def conservative_available_date(report_period: str) -> date:
        """Return the latest statutory-style availability date for a quarter."""
        try:
            year_text, quarter = report_period.split("-")
            year = int(year_text)
        except ValueError as error:
            raise ValueError("report_period must look like YYYY-QN") from error
        if quarter == "Q1":
            return date(year, 5, 1)
        if quarter == "Q2":
            return date(year, 9, 1)
        if quarter == "Q3":
            return date(year, 11, 1)
        if quarter == "Q4":
            return date(year + 1, 5, 1)
        raise ValueError("report_period must end with Q1, Q2, Q3, or Q4")


"""Pure allocation from explicit close snapshots, never signal-adjusted prices.

This module neither reads sources nor mutates an account. Limits apply to the
signal-close sizing amount; next-open price gaps still require execution-time
risk validation by the caller. It does not certify a backtest for release.
"""
@dataclass(frozen=True)
class AllocationLimits:
    lot_size: int = 100
    stock_weight: Decimal | None = None
    adv_fraction: Decimal | None = None


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


# --- order and fill orchestration ---
"""Order/fill orchestration over one authoritative account; no I/O or strategy.

The existing execution model receives an ephemeral *execution capacity* book,
not a second portfolio. Only AccountLedger postings change persistent cash and
positions. Corporate-action claims and taxes must be settled by the caller;
this boundary does not certify prices, limits, fee rounding or source coverage.
"""
class AccountExecutionSession:
    def __init__(self, account: AccountLedger, rules: TradeRules):
        self.account=account
        if not isinstance(rules, TradeRules):
            raise TypeError('TradeRules are required')
        self.rules=rules
        self._originals={}; self._pending={}; self._attempts={}; self._fills=[]
        self._targets={}
        self._open_attempts={}
        self._capacity_used={}

    @property
    def orders(self):
        return tuple(self._originals[key] for key in sorted(self._originals))

    @property
    def pending(self):
        return tuple(self._pending[key] for key in sorted(self._pending))

    @property
    def fills(self):
        return tuple(self._fills)

    def submit(self, order: Order):
        if order.order_id in self._originals:
            if self._originals[order.order_id]!=order:
                raise ValueError('conflicting submitted order')
            return
        self.account.register_order(order)
        self._originals[order.order_id]=order
        self._pending[order.order_id]=order

    def execute(self, order_id: str, bar: Bar, *, previous_close: float | None):
        """Low-level accounting test boundary; production uses execute_open risk checks."""
        key=(order_id,bar.trade_date); inputs=(bar,previous_close)
        if key in self._attempts:
            original_inputs,fill=self._attempts[key]
            if original_inputs!=inputs: raise ValueError('conflicting execution replay')
            return fill
        order=self._pending[order_id]
        fill=self._preview(order,bar,previous_close)
        return self._record_attempt(order,bar,fill,inputs)

    def _preview(self,order,bar,previous_close,reserve=0,previous20_mean_volume=None,used=0):
        positions={}; sellable={}
        for lot in self.account.lots:
            positions[lot.symbol]=positions.get(lot.symbol,0)+lot.shares
            if lot.tradable_from<=bar.trade_date:
                sellable[lot.symbol]=sellable.get(lot.symbol,0)+lot.shares
        price=self.rules.price(order,bar,0,previous_close,volume20=previous20_mean_volume)
        quantity=self.rules.quantity(order,bar,price=price,previous_close=previous_close,
                                     cash=self.account.cash,positions=positions,sellable=sellable,
                                     reserve=reserve,volume20=previous20_mean_volume,used=used)
        price=self.rules.price(order,bar,quantity,previous_close,
                               volume20=previous20_mean_volume)
        gross=price*quantity
        fee=self.rules.fees(gross,order.side,bar.trade_date,symbol=order.symbol,shares=quantity)
        total=gross+fee.commission+fee.stamp_duty+fee.transfer_fee
        if order.side is Side.BUY and total>self.account.cash-reserve:
            return Fill(order.order_id,order.symbol,order.side,bar.trade_date,0,0,0,0,0,
                        'rejected','insufficient_cash_or_shares')
        reason=self.rules.check(order,bar,previous_close,price=price,quantity=quantity,
                                positions=positions,sellable=sellable)
        if reason:
            return Fill(order.order_id,order.symbol,order.side,bar.trade_date,0,0,0,0,0,
                        'rejected',reason)
        return Fill(order.order_id,order.symbol,order.side,bar.trade_date,quantity,float(price),
                    float(gross),float(fee.commission),float(fee.stamp_duty),'filled',
                    transfer_fee=float(fee.transfer_fee))

    def _record_attempt(self,order,bar,fill,inputs):
        order_id=order.order_id; key=(order_id,bar.trade_date)
        if fill.status=='filled':
            self.account.record_fill(f'fill:{order_id}:{bar.trade_date.isoformat()}',fill)
        outstanding=remaining_order(order,fill)
        if outstanding is None:
            del self._pending[order_id]
        else:
            self._pending[order_id]=outstanding
        self._fills.append(fill); self._attempts[key]=(inputs,fill)
        return fill

    def cancel(self, order_id, signal_date, reason):
        if order_id not in self._originals: raise KeyError(order_id)
        self.account.record_cancel(f'cancel:{order_id}:{signal_date.isoformat()}',
                                   signal_date,order_id,reason)
        self._pending.pop(order_id,None)

    def execute_open(self, trade_date: date, bars: Mapping[str, Bar],
                     previous_closes: Mapping[str, float | None], *, risk: OpeningRiskSnapshot,
                     volume20: Mapping[str, Decimal] | None = None,
                     cash_reserve: Decimal = Decimal(0), on_fill=None, eligible=None):
        """Execute a complete pending-order snapshot, sells before buys.

        Missing bars are not interpreted as suspensions. The data adapter must
        supply explicit observations, including zero-volume bars when proved.
        Validate every required observation before changing any account state.
        This is not an all-or-nothing trade transaction: ordinary execution
        rejections remain individually audited and pending.
        """
        volume20 = {} if volume20 is None else dict(volume20)
        if callable(cash_reserve) or not isinstance(cash_reserve, Decimal):
            raise TypeError('cash_reserve must be an explicit Decimal')
        if not cash_reserve.is_finite() or cash_reserve < 0:
            raise ValueError('cash_reserve must be finite and nonnegative')
        eligible = None if eligible is None else tuple(eligible)
        inputs=(tuple(sorted(bars.items())),tuple(sorted(previous_closes.items())),risk.liquidity_as_of,
                tuple(sorted(risk.raw_marks.items())),tuple(sorted(risk.amount20.items())),
                tuple(sorted(volume20.items())),cash_reserve,risk.receivables,risk.liabilities,risk.limits,
                eligible)
        if trade_date in self._open_attempts:
            original,fills=self._open_attempts[trade_date]
            if original!=inputs: raise ValueError('conflicting opening replay')
            return fills
        excluded = ()
        if eligible is not None:
            allowed = set(eligible)
            excluded = tuple(order for order in self.pending
                             if order.side == Side.BUY and order.symbol not in allowed)
        orders=sorted((order for order in self.pending if order not in excluded),
                      key=lambda order:(order.side!=Side.SELL,order.order_id))
        holdings=self._holdings()
        self.rules.check(phase='opening_validate', snapshot=risk,
                         trade_date=trade_date, holdings=holdings, orders=orders)
        for order in orders:
            if order.symbol not in bars or order.symbol not in previous_closes:
                raise ValueError(f'missing opening observation: {order.symbol}')
            bar=bars[order.symbol]
            if bar.symbol!=order.symbol or bar.trade_date!=trade_date:
                raise ValueError(f'opening observation identity/date mismatch: {order.symbol}')
            if bar.open>0 and nonnegative(risk.raw_marks[order.symbol])!=nonnegative(bar.open):
                raise ValueError(f'opening mark does not match executable open: {order.symbol}')
            if (order.order_id,trade_date) in self._attempts:
                raise ValueError('opening batch overlaps an existing individual attempt')
        for order in excluded:
            self.cancel(order.order_id, trade_date, 'market_ineligible')
        fills=[]
        for order in orders:
            bar=bars[order.symbol]; previous=previous_closes[order.symbol]
            used=self._capacity_used.get((trade_date, order.symbol), 0)
            reserve=cash_reserve + self.rules.tax(trade_date, phase='reserve')
            fill=self._preview(order,bar,previous,reserve,volume20.get(order.symbol),used)
            reason=self.rules.check(
                phase='opening_rejection', order=order, fill=fill,
                equity=self.rules.check(phase='opening_equity', cash=self.account.cash,
                                        holdings=self._holdings(), snapshot=risk),
                holdings=self._holdings(), snapshot=risk)
            if reason:
                fill=Fill(order.order_id,order.symbol,order.side,trade_date,0,0,0,0,0,'rejected',reason)
            tax_active = self.rules._tax_ledger._opening is not None
            tax_state = self.rules._tax_snapshot() if tax_active else None
            try:
                if tax_active:
                    self.rules.tax(trade_date, (fill,), phase='preview')
                if on_fill is not None:
                    on_fill(fill)
                recorded=self._record_attempt(order,bar,fill,(bar,previous,inputs))
            except Exception:
                if tax_active:
                    self.rules._tax_restore(tax_state)
                raise
            fills.append(recorded)
            if fill.status == 'filled':
                self._capacity_used[(trade_date, order.symbol)] = used + fill.shares
        result=tuple(fills); self._open_attempts[trade_date]=(inputs,result)
        return result

    def _holdings(self):
        holdings={}
        for lot in self.account.lots:
            holdings[lot.symbol]=holdings.get(lot.symbol,0)+lot.shares
        return holdings

    def apply_target(self, target, *, next_session, equity, raw_closes, amount20,
                     limits=AllocationLimits(), rebalance=True):
        """Allocate first; only a validated replacement cancels old pending orders."""
        key=(target.source_id,target.as_of)
        inputs=(target.source_id,target.as_of,tuple(sorted(target.weights.items())),
                tuple(sorted(target.reasons.items())),rebalance,
                next_session,equity,tuple(sorted(raw_closes.items())),tuple(sorted(amount20.items())),limits)
        if key in self._targets:
            original,plan=self._targets[key]
            if original!=inputs: raise ValueError('conflicting target replay')
            return plan
        holdings=self._holdings()
        plan=self.rules.allocate(target,next_session=next_session,equity=equity,
                                 holdings=holdings,raw_closes=raw_closes,amount20=amount20,limits=limits,
                                 rebalance=rebalance)
        if rebalance:
            for order in self.pending:
                self.cancel(order.order_id,target.as_of.date(),target.reasons.get('__market__','target_replaced'))
            for order in plan.orders: self.submit(order)
        self._targets[key]=(inputs,plan)
        return plan


# --- account state and postings ---
"""Exact event account; no prices, signals, tax inference, execution or I/O.

Receivable/payable valuations must be supplied explicitly at every mark.
Corporate-action entitlements and taxes must be validated upstream. This
component alone does not certify corporate-action completeness or NAV.
"""


def _money(value):
    number=Decimal(str(value))
    if not number.is_finite(): raise ValueError('finite money required')
    return number


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    symbol: str
    acquired: date
    tradable_from: date
    shares: int
    cost: Decimal


@dataclass(frozen=True)
class CashPosting:
    event_id: str
    day: date
    kind: str
    cash_delta: Decimal
    cash_after: Decimal
    source: str


@dataclass(frozen=True)
class OrderCancellation:
    order_id: str
    day: date
    reason: str


@dataclass(frozen=True)
class AccountPoint:
    day: date
    cash: Decimal
    market_value: Decimal
    receivables: Decimal
    liabilities: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    net_cash_income: Decimal


class AccountLedger:
    def __init__(self, initial_cash: Decimal):
        self.initial_cash=_money(initial_cash)
        if self.initial_cash<=0: raise ValueError('positive initial cash required')
        self._cash=self.initial_cash; self._realized=Decimal(0); self._income=Decimal(0)
        self._lots=(); self._orders={}; self._filled={}; self._events={}
        self._postings=[]; self._day=None
        self._cancelled={}

    @property
    def cash(self): return self._cash

    @property
    def as_of(self): return self._day

    @property
    def lots(self): return self._lots

    @property
    def cash_events(self): return tuple(self._postings)

    @property
    def order_cancellations(self): return tuple(self._cancelled.values())

    def _time(self,day):
        if not isinstance(day,date) or (self._day is not None and day<self._day):
            raise ValueError('account time cannot move backwards')

    def _replay(self,key,payload):
        if not isinstance(key,str) or not key: raise ValueError('event identity required')
        if key not in self._events: return False
        if self._events[key]!=payload: raise ValueError('conflicting event replay')
        return True

    def _post(self,event_id,day,kind,delta,source):
        self._cash+=delta
        self._postings.append(CashPosting(event_id,day,kind,delta,self._cash,source))

    def register_order(self,order: Order):
        if (not order.order_id or not order.symbol or type(order.requested_shares) is not int
                or order.requested_shares<=0 or order.submit_date<=order.signal_date):
            raise ValueError('valid sourced next-session order required')
        if order.order_id in self._orders and self._orders[order.order_id]!=order:
            raise ValueError('conflicting order identity')
        self._orders[order.order_id]=order

    def record_cancel(self,event_id,day,order_id,reason):
        payload=OrderCancellation(order_id,day,reason)
        if self._replay(event_id,payload): return
        self._time(day)
        if order_id not in self._orders: raise ValueError('cancel without order')
        order=self._orders[order_id]
        if (not isinstance(reason,str) or not reason.strip() or day<order.signal_date
                or order_id in self._cancelled
                or self._filled.get(order_id,0)>=order.requested_shares):
            raise ValueError('invalid or conflicting order cancellation')
        self._cancelled[order_id]=payload; self._events[event_id]=payload; self._day=day

    def record_fill(self,event_id: str,fill: Fill):
        if self._replay(event_id,fill): return
        self._time(fill.trade_date)
        if fill.order_id not in self._orders: raise ValueError('fill without order')
        order=self._orders[fill.order_id]
        if fill.order_id in self._cancelled:
            raise ValueError('fill for cancelled order')
        if (fill.symbol!=order.symbol or fill.side!=order.side or fill.status!='filled'
                or fill.trade_date<order.submit_date or fill.trade_date<=order.signal_date
                or type(fill.shares) is not int or fill.shares<=0):
            raise ValueError('fill does not match order')
        filled=self._filled.get(fill.order_id,0)+fill.shares
        if filled>order.requested_shares: raise ValueError('order overfill')
        price,gross,commission,stamp,transfer=map(_money,(fill.price,fill.gross_amount,fill.commission,fill.stamp_duty,fill.transfer_fee))
        if (price<=0 or gross<=0 or commission<0 or stamp<0 or transfer<0
                or abs(price*fill.shares-gross)>Decimal('0.000001')):
            raise ValueError('fill monetary terms inconsistent')
        fees=commission+stamp+transfer
        if fill.side is Side.BUY:
            if stamp!=0 or gross+fees>self._cash: raise ValueError('invalid buy tax or insufficient cash')
            lots=self._lots+(PositionLot(event_id,fill.symbol,fill.trade_date,
                fill.trade_date+timedelta(days=1),fill.shares,gross+fees),)
            delta=-gross
            realized=self._realized
        elif fill.side is Side.SELL:
            available=sum(l.shares for l in self._lots if l.symbol==fill.symbol and l.tradable_from<=fill.trade_date)
            if available<fill.shares: raise ValueError('oversell or T+1 locked shares')
            needed=fill.shares; removed=Decimal(0); lots=[]
            for lot in sorted(self._lots,key=lambda l:l.acquired):
                take=min(needed,lot.shares) if lot.symbol==fill.symbol and lot.tradable_from<=fill.trade_date else 0
                cost=lot.cost*Decimal(take)/Decimal(lot.shares)
                removed+=cost; needed-=take
                if take<lot.shares: lots.append(replace(lot,shares=lot.shares-take,cost=lot.cost-cost))
            lots=tuple(lots); delta=gross; realized=self._realized+gross-fees-removed
            if self._cash+gross-fees<0: raise ValueError('insufficient cash to settle sell')
        else: raise ValueError('unsupported fill side')
        self._post(event_id,fill.trade_date,fill.side.value,delta,fill.order_id)
        if commission: self._post(event_id,fill.trade_date,'commission',-commission,fill.order_id)
        if stamp: self._post(event_id,fill.trade_date,'stamp_duty',-stamp,fill.order_id)
        if transfer: self._post(event_id,fill.trade_date,'transfer_fee',-transfer,fill.order_id)
        self._lots=lots; self._realized=realized; self._filled[fill.order_id]=filled
        self._events[event_id]=fill; self._day=fill.trade_date

    def record_cash(self,event_id,day,kind,amount,source):
        amount=_money(amount); payload=('cash',day,kind,amount,source)
        if self._replay(event_id,payload): return
        self._time(day)
        if not isinstance(source,str) or not re.fullmatch('[0-9a-f]{64}',source):
            raise ValueError('cash source SHA-256 required')
        if ((kind=='dividend' and amount<0) or (kind in {'dividend_tax','transfer_fee'} and amount>0)
                or kind not in {'dividend','dividend_tax','transfer_fee'} or self._cash+amount<0):
            raise ValueError('invalid signed cash event or insufficient cash')
        self._post(event_id,day,kind,amount,source)
        self._income+=amount; self._events[event_id]=payload; self._day=day

    def record_share_credit(self,event_id,day,symbol,shares,acquired,source,*,tradable_from):
        payload=('shares',day,symbol,shares,acquired,source,tradable_from)
        if self._replay(event_id,payload): return
        self._time(day)
        if (not symbol or type(shares) is not int or shares<=0
                or not isinstance(acquired,date) or acquired>day
                or not isinstance(tradable_from,date) or tradable_from<day
                or not isinstance(source,str) or not re.fullmatch('[0-9a-f]{64}',source)):
            raise ValueError('actual integral share entitlement and source required')
        self._lots+=(PositionLot(event_id,symbol,acquired,tradable_from,shares,Decimal(0)),)
        self._events[event_id]=payload; self._day=day

    def mark(self,day,raw_prices,receivables: Decimal,liabilities: Decimal):
        self._time(day)
        receivables,liabilities=map(_money,(receivables,liabilities))
        if min(receivables,liabilities)<0: raise ValueError('nonnegative explicit claims/liabilities required')
        value=Decimal(0); cost=Decimal(0)
        for lot in self._lots:
            if lot.symbol not in raw_prices: raise ValueError('held position lacks raw mark')
            price=_money(raw_prices[lot.symbol])
            if price<0: raise ValueError('nonnegative raw mark required')
            value+=price*lot.shares; cost+=lot.cost
        point=AccountPoint(day,self._cash,value,receivables,liabilities,
            self._cash+value+receivables-liabilities,self._realized,value-cost,self._income)
        if (sum((p.cash_delta for p in self._postings),self.initial_cash)!=self._cash
                or abs(point.equity-self.initial_cash-self._realized-(value-cost)-self._income-receivables+liabilities)>Decimal('1e-18')):
            raise ValueError('account conservation invariant failed')
        self._day=day
        return point


# --- execution fill state ---


def remaining_order(order: Order, fill: Fill) -> Order | None:
    """Retain unfilled quantity with the original signal and order identity."""
    if (fill.order_id, fill.symbol, fill.side) != (order.order_id, order.symbol, order.side):
        raise ValueError('fill does not belong to order')
    if fill.shares < 0 or fill.shares > order.requested_shares:
        raise ValueError('fill quantity outside order bounds')
    remaining = order.requested_shares - fill.shares
    return replace(order, requested_shares=remaining) if remaining else None


# --- opening execution risk ---
"""Pure execution-risk contract; no strategy, source or account mutation."""
@dataclass(frozen=True)
class OpeningRiskSnapshot:
    liquidity_as_of: date
    raw_marks: Mapping[str,Decimal]
    amount20: Mapping[str,Decimal]
    receivables: Decimal
    liabilities: Decimal
    limits: AllocationLimits = AllocationLimits()
    opening_volume20: Mapping[str, Decimal] = field(default_factory=dict)


def nonnegative(value):
    value=Decimal(str(value))
    if not value.is_finite() or value<0:
        raise ValueError('finite nonnegative opening risk input required')
    return value


# --- dated fee policy ---
"""Explicit dated fee policy; dates outside supplied schedules are rejected."""

@dataclass(frozen=True)
class FeePolicy:
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_rates: tuple
    transfer_rates: tuple
    source_sha256: str
    rounding: str = 'none'

    @classmethod
    def from_mapping(cls, raw, source_sha256):
        if raw['rounding'] not in {'none', 'half_up'} or not re.fullmatch('[0-9a-f]{64}',source_sha256):
            raise ValueError('explicit supported rounding and source hash required')
        def schedule(key):
            rows=tuple((date.fromisoformat(r['effective']),Decimal(r['rate'])) for r in raw[key])
            if not rows or tuple(sorted(set(d for d,_ in rows)))!=tuple(d for d,_ in rows):
                raise ValueError('ordered unique effective fee dates required')
            if any(not r.is_finite() or r<0 for _,r in rows): raise ValueError('invalid fee rate')
            return rows
        rate,minimum=Decimal(raw['commission_rate']),Decimal(raw['minimum_commission'])
        if any(not v.is_finite() or v<0 for v in (rate,minimum)): raise ValueError('invalid commission')
        transfers=[]
        for row in raw['transfer_rates']:
            effective=date.fromisoformat(row['effective'])
            value=Decimal(row['rate']); basis=row.get('basis','amount'); market=row.get('market')
            if not value.is_finite() or value<0 or basis not in {'amount','shares'} or (market is not None and (not isinstance(market,str) or not market)):
                raise ValueError('invalid transfer fee terms')
            transfers.append((effective,value,basis,market))
        keys=[(item[0],item[3] or '') for item in transfers]
        if not transfers or keys!=sorted(set(keys)):
            raise ValueError('ordered unique transfer fee dates and markets required')
        return cls(rate,minimum,schedule('stamp_rates'),tuple(transfers),source_sha256,raw['rounding'])

@dataclass(frozen=True)
class FeeBreakdown:
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal


class TradeRules:
    """One public rule surface; each action selects its own declared model."""

    def __init__(self, execution, fee_policy, tax_policy):
        normalized = validate_execution(execution)
        self.price_config = normalized['price']
        self.quantity_config = normalized['quantity']
        self.fees_config = normalized['fees']
        self.tax_config = normalized['tax']
        self.check_config = normalized['check']
        if not isinstance(fee_policy, FeePolicy) or not isinstance(tax_policy, TaxPolicy):
            raise TypeError('fee and tax policies are required')
        self.fee_policy = fee_policy
        self.tax_policy = tax_policy
        self._tax_ledger = TaxLedger(tax_policy)

    def allocate(self, target: TargetSet, *, next_session: date,
                 equity: Decimal, holdings: Mapping[str, int],
                 raw_closes: Mapping[str, Decimal], amount20: Mapping[str, Decimal],
                 limits: AllocationLimits = AllocationLimits(), rebalance: bool = True) -> AllocationPlan:
        if next_session <= target.as_of.date():
            raise ValueError('next-session execution date required')
        if not rebalance:
            return AllocationPlan((), ())
        equity = _positive(equity)
        if type(limits.lot_size) is not int or limits.lot_size <= 0:
            raise ValueError('positive integer lot size required')
        weight_cap = None if limits.stock_weight is None else _positive(limits.stock_weight)
        adv_cap = None if limits.adv_fraction is None else _positive(limits.adv_fraction)
        if any(cap is not None and cap > 1 for cap in (weight_cap, adv_cap)):
            raise ValueError('risk fractions cannot exceed one')
        weights = {symbol: Decimal(str(weight)) for symbol, weight in target.weights.items()}
        exposure = sum(weights.values(), Decimal(0))
        if (not exposure.is_finite() or not 0 <= exposure <= 1
                or any(not weight.is_finite() or weight < 0 for weight in weights.values())):
            raise ValueError('finite unleveraged consistent target weights required')
        if any(type(number) is not int or number < 0 for number in holdings.values()):
            raise ValueError('integral nonnegative holdings required')
        symbols = sorted(set(holdings) | set(weights))
        observations = {}
        for symbol in symbols:
            if symbol not in raw_closes or symbol not in amount20:
                raise ValueError(f'missing allocation observation: {symbol}')
            observations[symbol] = (_positive(raw_closes[symbol]), _positive(amount20[symbol]))
        orders, decisions = [], []
        for symbol in symbols:
            price, adv = observations[symbol]
            weight = weights.get(symbol, Decimal(0))
            desired = int(equity * weight / price // limits.lot_size) * limits.lot_size
            existing = holdings.get(symbol, 0)
            delta = desired - existing
            requested = delta // limits.lot_size * limits.lot_size if delta > 0 else abs(delta)
            reason = None
            if weight_cap is not None and weight > weight_cap:
                reason = 'weight_above_stock_limit'
            elif (weight > 0 and desired == 0 and existing == 0) or (delta > 0 and requested == 0):
                reason = 'below_one_lot'
            elif adv_cap is not None and requested * price > adv * adv_cap:
                reason = 'order_above_adv_limit'
            if reason:
                decisions.append(AllocationDecision(symbol, desired, delta, 'skipped', reason))
            elif delta == 0:
                decisions.append(AllocationDecision(symbol, desired, delta, 'unchanged', 'already_at_target'))
            else:
                side = Side.BUY if delta > 0 else Side.SELL
                reason = target.reasons.get(symbol, 'target_exit')
                identity = repr((target.source_id, target.as_of.date(), next_session, symbol,
                                 side.value, requested, reason))
                orders.append(Order(sha256(identity.encode()).hexdigest(), symbol, side, requested,
                                    target.as_of.date(), next_session, reason))
                decisions.append(AllocationDecision(symbol, desired, delta, 'ordered', reason))
        orders.sort(key=lambda order: (order.side != Side.SELL, order.symbol))
        return AllocationPlan(tuple(orders), tuple(decisions))

    @staticmethod
    def _decimal(value, name, *, positive=False):
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
            raise ValueError(f'{name} must be finite and valid')
        return result

    def price(self, order, bar, quantity, previous_close, *, volume20=None):
        params = self.price_config['params']; model = self.price_config['model']
        if type(quantity) is not int or quantity < 0:
            raise ValueError('price quantity must be a nonnegative integer')
        opening = self._decimal(bar.open, 'bar.open', positive=True)
        if model == 'ratio':
            rate = self._decimal(params['rate'], 'rate')
            raw = opening * (1 + rate if order.side is Side.BUY else 1 - rate)
        elif model == 'tick':
            tick = self._decimal(params['tick'], 'tick', positive=True)
            ticks = params['ticks']
            if type(ticks) is not int or ticks < 0:
                raise ValueError('ticks must be a nonnegative integer')
            raw = opening + (tick * ticks if order.side is Side.BUY else -tick * ticks)
        elif model == 'volume_share':
            impact = self._decimal(params['impact'], 'impact')
            if params.get('capacity_basis') != 'prior20' or volume20 is None:
                raise ValueError('prior20 volume20 is required')
            reference = self._decimal(volume20, 'volume20', positive=True)
            share = Decimal(quantity) / reference
            move = impact * share * share
            raw = opening * (1 + move if order.side is Side.BUY else 1 - move)
        else:
            raise ValueError(f'unsupported price model: {model}')
        tick = self._decimal(params['tick'], 'tick', positive=True)
        rounding = ROUND_CEILING if order.side is Side.BUY else ROUND_FLOOR
        value = (raw / tick).to_integral_value(rounding=rounding) * tick
        if value <= 0:
            raise ValueError('calculated price must be positive')
        return value

    def quantity(self, order, bar, *, price, cash, positions, sellable, reserve,
                 previous_close=None, volume20=None, used=0):
        params = self.quantity_config['params']; model = self.quantity_config['model']
        min_qty = self.check_config['params']['min_qty']
        step = self.check_config['params']['step']
        if any(type(value) is not int or value <= 0 for value in (min_qty, step)):
            raise ValueError('min_qty and step must be positive integers')
        if self.price_config['model']=='volume_share' and order.side is Side.BUY:
            result = self._joint_quantity(order,bar,previous_close,cash,positions,sellable,reserve,
                                          volume20,used,min_qty,step)
        else:
            result = self._quantity_limit(order,bar,price,cash,positions,sellable,reserve,
                                          volume20,used,step)
        result = min(result, order.requested_shares)
        available_sell = int(sellable.get(order.symbol, 0))
        if order.side is Side.SELL and result == available_sell and result > 0:
            return result
        if (order.side is Side.SELL and 0 < result < min_qty
                and self._sell_remainder_is_legal(available_sell, result, min_qty, step)):
            return result
        if result < min_qty:
            return 0
        return min_qty + ((result - min_qty) // step) * step

    @staticmethod
    def _sell_remainder_is_legal(available, quantity, min_qty, step):
        remainder = available - quantity
        return remainder == 0 or (remainder >= min_qty and (remainder - min_qty) % step == 0)

    def _quantity_limit(self, order, bar, price, cash, positions, sellable, reserve,
                        volume20, used, step):
        params=self.quantity_config['params']; model=self.quantity_config['model']
        if model == 'capacity':
            return min(order.requested_shares, self._capacity(order,bar,price,cash,sellable,reserve,step))
        if params.get('capacity_basis') != 'prior20' or volume20 is None:
            raise ValueError('prior20 volume20 is required')
        participation=self._decimal(params['participation'],'participation',positive=True)
        result=max(0,int(self._decimal(volume20,'volume20',positive=True)*participation)-used)
        result=min(order.requested_shares,result)
        if order.side is Side.BUY:
            result=min(result,self._capacity(order,bar,price,cash,sellable,reserve,step))
        else:
            result=min(result,int(sellable.get(order.symbol,0)))
        return result

    def _joint_quantity(self, order, bar, previous_close, cash, positions, sellable, reserve,
                        volume20, used, min_qty, step):
        if order.requested_shares < min_qty:
            return 0
        low, high = -1, (order.requested_shares-min_qty) // step
        available=self._decimal(cash,'cash')-self._decimal(reserve,'reserve')
        while low < high:
            middle=(low+high+1)//2
            candidate=min_qty+middle*step
            candidate_price=self.price(order,bar,candidate,previous_close,volume20=volume20)
            limit=self._quantity_limit(order,bar,candidate_price,cash,positions,sellable,reserve,
                                       volume20,used,step)
            fee=self.fees(candidate_price*candidate,order.side,bar.trade_date,symbol=order.symbol,shares=candidate)
            total=candidate_price*candidate+fee.commission+fee.stamp_duty+fee.transfer_fee
            if candidate <= limit and total <= available:
                low=middle
            else:
                high=middle-1
        return 0 if low < 0 else min_qty+low*step

    def _capacity(self, order, bar, price, cash, sellable, reserve, step):
        if order.side is Side.SELL:
            return max(0, int(sellable.get(order.symbol, 0)))
        available = max(Decimal(0), self._decimal(cash, 'cash') - self._decimal(reserve, 'reserve'))
        limit = max(0, order.requested_shares // step)
        low, high = 0, limit
        while low < high:
            middle = (low + high + 1) // 2
            quantity = middle * step
            gross = price * quantity
            fee = self.fees(gross, order.side, bar.trade_date,symbol=order.symbol,shares=quantity)
            total = gross + fee.commission + fee.stamp_duty + fee.transfer_fee
            if total <= available:
                low = middle
            else:
                high = middle - 1
        return low * step

    def fees(self, gross, side, day, *, symbol=None, shares=None):
        rounding = self.fees_config['params']['rounding']
        gross = self._decimal(gross, 'gross')
        def rate(rows):
            eligible = [value for effective, value in rows if effective <= day]
            if not eligible:
                raise ValueError('fee effective date does not cover trade')
            return eligible[-1]
        stamp = rate(self.fee_policy.stamp_rates)
        market=symbol.split('.',1)[0] if isinstance(symbol,str) and '.' in symbol else None
        terms=[row for row in self.fee_policy.transfer_rates
               if row[0]<=day and (row[3] is None or row[3]==market)]
        if not terms:
            raise ValueError('transfer fee date or market is not covered')
        _,transfer,basis,_=terms[-1]
        base=gross if basis=='amount' else self._decimal(shares,'shares')
        result = FeeBreakdown(
            max(self.fee_policy.minimum_commission, gross * self.fee_policy.commission_rate),
            gross * stamp if side is Side.SELL else Decimal(0),
            base * transfer,
        )
        if rounding == 'half_up':
            result = FeeBreakdown(*(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) for value in (
                result.commission, result.stamp_duty, result.transfer_fee)))
        return result

    def _tax_snapshot(self):
        state = {}
        for name, value in self._tax_ledger.__dict__.items():
            if isinstance(value, dict):
                state[name] = dict(value)
            elif isinstance(value, list):
                state[name] = list(value)
            else:
                state[name] = value
        return state

    def _tax_restore(self, state):
        self._tax_ledger.__dict__.clear()
        self._tax_ledger.__dict__.update(state)

    def tax(self, day=None, fills=(), *, phase, acquired=None, disposed=None,
            registration=None, taxable_income=None, long_rate=None):
        if self.tax_config['model'] != 'fifo':
            raise ValueError('tax model must be fifo')
        if phase == 'begin':
            self._tax_ledger.begin(day)
            return None
        if phase == 'preview':
            for fill in fills:
                self._tax_preview(fill)
            return None
        if phase == 'end':
            self._tax_end(tuple(fills))
            return None
        if phase == 'liability':
            return self._tax_liability(day)
        if phase == 'reserve':
            return self._tax_ledger.reserve(day)
        if phase == 'rate':
            return self._holding_rate(acquired, disposed, long_rate)
        if phase == 'amount':
            return self._dividend_tax(acquired, disposed, registration, taxable_income)
        raise ValueError('unsupported fifo tax phase')

    @staticmethod
    def _anniversary(acquired: date, months: int) -> date:
        ordinal = acquired.year * 12 + acquired.month - 1 + months
        year, month = divmod(ordinal, 12)
        month += 1
        return date(year, month, min(acquired.day, monthrange(year, month)[1]))

    def _holding_rate(self, acquired: date, disposed: date, long_rate: Decimal) -> Decimal:
        if (not isinstance(acquired, date) or not isinstance(disposed, date)
                or disposed < acquired or not isinstance(long_rate, Decimal)
                or not long_rate.is_finite() or long_rate < 0):
            raise ValueError('valid holding dates and declared long-term tax rate required')
        if disposed <= self._anniversary(acquired, 1):
            return Decimal('.20')
        if disposed <= self._anniversary(acquired, 12):
            return Decimal('.10')
        return long_rate

    def _dividend_tax(self, acquired, disposed, registration, taxable_income) -> TaxResult:
        if (not all(isinstance(day, date) for day in (acquired, disposed, registration))
                or not acquired <= registration < disposed or registration <= date(2013, 1, 1)):
            raise ValueError('valid acquisition, record-close and disposal dates required; pre2013 unsupported')
        if (not isinstance(taxable_income, Decimal) or not taxable_income.is_finite()
                or taxable_income < 0):
            raise ValueError('finite nonnegative Decimal taxable income required')
        long_rate, withholding_rate, policy = self.tax_policy.terms(registration)
        rate = self._holding_rate(acquired, disposed, long_rate)
        withheld = taxable_income * withholding_rate
        final = taxable_income * rate
        return TaxResult(taxable_income, rate, withheld, final, final - withheld,
                         policy)

    def _allocate_fifo(self, lots: tuple[TaxLot, ...], net_disposed_shares: int,
                       disposed: date) -> tuple[tuple[TaxLot, ...], tuple[TaxLot, ...]]:
        if (type(net_disposed_shares) is not int or net_disposed_shares < 0
                or not isinstance(disposed, date)):
            raise ValueError('nonnegative integer net disposal and date required')
        if len({lot.lot_id for lot in lots}) != len(lots):
            raise ValueError('duplicate tax lot identity')
        if any(not lot.lot_id or type(lot.shares) is not int or lot.shares <= 0
               or not isinstance(lot.acquired, date) or lot.acquired > disposed for lot in lots):
            raise ValueError('invalid tax lots')
        if sum(lot.shares for lot in lots) < net_disposed_shares:
            raise ValueError('net disposal exceeds holdings')
        indexed = sorted(enumerate(lots), key=lambda item: (item[1].acquired, item[0]))
        allocated, remaining, needed = [], [], net_disposed_shares
        for _, lot in indexed:
            take = min(needed, lot.shares)
            if take:
                allocated.append(replace(lot, shares=take))
            if take < lot.shares:
                remaining.append(replace(lot, shares=lot.shares - take))
            needed -= take
        return tuple(allocated), tuple(remaining)

    def _tax_sell(self, symbol, shares, day):
        ledger=self._tax_ledger
        symbol_lots = tuple(lot for lot in ledger.lots if lot.symbol == symbol)
        allocated, remaining_symbol = self._allocate_fifo(symbol_lots, shares, day)
        takes = {lot.lot_id: lot.shares for lot in allocated}
        remaining_by_id = {lot.lot_id: lot for lot in remaining_symbol}
        remaining=[]
        for lot in sorted(ledger.lots, key=lambda lot: lot.acquired):
            take = takes.get(lot.lot_id, 0)
            for right in lot.rights:
                tax_result=self._dividend_tax(lot.acquired,day,right.registration,
                    right.income_per_share*take)
                tax=tax_result.supplement
                if tax:
                    key=canonical_hash((right.distribution_id,right.component,lot.lot_id,day.isoformat()))
                    old=ledger.pending[key].amount if key in ledger.pending else Decimal(0)
                    ledger.pending[key]=TaxPayment(key,right.distribution_id,symbol,lot.acquired,day,right.operation,
                        max(day,right.due),old+tax,right.source_sha256,tax_result.policy)
            if lot.symbol == symbol:
                if lot.lot_id in remaining_by_id:
                    remaining.append(remaining_by_id[lot.lot_id])
            else:
                remaining.append(lot)
        ledger.lots=tuple(remaining)

    def _tax_preview(self, fill):
        ledger=self._tax_ledger
        if ledger._opening is None or fill.trade_date!=ledger._day:
            raise ValueError('tax preview outside current session')
        if fill.status=='filled' and fill.side is Side.SELL:
            self._tax_sell(fill.symbol,fill.shares,fill.trade_date)

    def _tax_end(self, fills):
        ledger=self._tax_ledger
        if ledger._opening is None or any(fill.trade_date!=ledger._day for fill in fills):
            raise ValueError('tax daily net settlement requires current fills')
        ledger.lots,ledger.pending=ledger._opening; ledger.pending=dict(ledger.pending)
        symbols=sorted({fill.symbol for fill in fills if fill.status=='filled'})
        for symbol in symbols:
            buys=sum(fill.shares for fill in fills if fill.symbol==symbol and fill.status=='filled' and fill.side is Side.BUY)
            sells=sum(fill.shares for fill in fills if fill.symbol==symbol and fill.status=='filled' and fill.side is Side.SELL)
            if sells>buys:
                self._tax_sell(symbol,sells-buys,ledger._day)
            elif buys>sells:
                identity=canonical_hash((symbol,ledger._day.isoformat(),tuple(fill.order_id for fill in fills if fill.symbol==symbol)))
                ledger.lots += (TaxHolding(identity,symbol,ledger._day,buys-sells),)
        ledger._opening=None

    def _tax_liability(self, day):
        ledger=self._tax_ledger
        potential=sum((self._dividend_tax(lot.acquired,day,right.registration,
                           lot.shares*right.income_per_share).supplement
                       for lot in ledger.lots for right in lot.rights if right.operation<=day), Decimal(0))
        return potential + ledger.reserve(day)

    def check(self, order=None, bar=None, previous_close=None, *, price=None, quantity=None,
              positions=None, sellable=None, phase='order', snapshot=None, trade_date=None,
              holdings=None, orders=None, equity=None, fill=None, cash=None,
              dataset=None, start=None):
        if phase == 'opening_validate':
            if not isinstance(snapshot.liquidity_as_of, date) or snapshot.liquidity_as_of >= trade_date:
                raise ValueError('liquidity must be observed before execution day')
            for symbol in set(holdings) | {item.symbol for item in orders}:
                if symbol not in snapshot.raw_marks:
                    raise ValueError(f'missing opening mark: {symbol} on {trade_date}')
                nonnegative(snapshot.raw_marks[symbol])
            for item in orders:
                if item.symbol not in snapshot.amount20:
                    raise ValueError(f'missing observed liquidity: {item.symbol}')
                nonnegative(snapshot.amount20[item.symbol])
            for value in (snapshot.receivables, snapshot.liabilities):
                nonnegative(value)
            for value in (snapshot.limits.stock_weight, snapshot.limits.adv_fraction):
                if value is not None and not 0 < nonnegative(value) <= 1:
                    raise ValueError('opening risk fractions must be in (0,1]')
            return None
        if phase == 'opening_equity':
            return (cash + sum((nonnegative(snapshot.raw_marks[symbol]) * shares
                                for symbol, shares in holdings.items()), Decimal(0))
                    + nonnegative(snapshot.receivables) - nonnegative(snapshot.liabilities))
        if phase == 'opening_rejection':
            if fill.status != 'filled':
                return None
            if (snapshot.limits.adv_fraction is not None
                    and Decimal(str(fill.price)) * fill.shares
                    > nonnegative(snapshot.amount20[order.symbol])
                    * nonnegative(snapshot.limits.adv_fraction)):
                return 'order_above_adv_limit'
            if order.side is Side.BUY:
                mark = nonnegative(snapshot.raw_marks[order.symbol])
                post_equity = (equity - Decimal(str(fill.gross_amount))
                                - Decimal(str(fill.commission))
                                - Decimal(str(fill.stamp_duty))
                                - Decimal(str(fill.transfer_fee)) + fill.shares * mark)
                post_value = (holdings.get(order.symbol, 0) + fill.shares) * mark
                if (post_equity <= 0 or
                        (snapshot.limits.stock_weight is not None
                         and post_value > post_equity * nonnegative(snapshot.limits.stock_weight))):
                    return 'position_above_stock_limit'
            return None
        if phase == 'dataset':
            needs_volume = (self.price_config['model'] == 'volume_share'
                            or self.quantity_config['model'] == 'participation')
            if not needs_volume:
                return None
            for record in dataset.records:
                if record.trade_date < start:
                    continue
                for symbol in record.bars:
                    volume20 = record.opening_volume20.get(symbol)
                    if volume20 is None or volume20 <= 0:
                        raise ValueError(
                            f'opening_volume20 is required and positive for {symbol} on {record.trade_date}'
                        )
            return None
        if phase != 'order':
            raise ValueError(f'unsupported check phase: {phase}')
        if positions is None or sellable is None:
            raise ValueError('positions and sellable are required')
        params = self.check_config['params']
        for name in ('tick', 'min_qty', 'step'):
            if name not in params:
                raise ValueError(f'missing explicit check parameter: {name}')
        tick = self._decimal(params['tick'], 'tick', positive=True)
        min_qty = params['min_qty']; step = params['step']
        if type(min_qty) is not int or type(step) is not int or min_qty <= 0 or step <= 0:
            raise ValueError('min_qty and step must be positive integers')
        if order.symbol != bar.symbol:
            return 'symbol_mismatch'
        if type(order.requested_shares) is not int or order.requested_shares <= 0:
            return 'invalid_order_quantity'
        if bar.trade_date <= order.signal_date:
            return 'trade_before_signal_date'
        if bar.trade_date < order.submit_date:
            return 'trade_before_submit_date'
        if not math.isfinite(bar.open) or bar.open <= 0:
            return 'invalid_price'
        if not math.isfinite(bar.volume) or bar.volume <= 0:
            return 'suspended_or_zero_volume'
        if price is not None and price <= 0:
            return 'invalid_price'
        if quantity is not None and quantity <= 0:
            return 'insufficient_capacity'
        if price is not None:
            if (Decimal(str(price)) / tick) % 1 != 0:
                return 'price_off_tick'
        full_sell = (order.side is Side.SELL and quantity is not None
                     and quantity == sellable.get(order.symbol, 0) and quantity > 0)
        odd_sell = (order.side is Side.SELL and quantity is not None and 0 < quantity < min_qty
                    and self._sell_remainder_is_legal(int(sellable.get(order.symbol, 0)),
                                                       quantity, min_qty, step))
        if quantity is not None and not full_sell and not odd_sell and (quantity < min_qty or (quantity - min_qty) % step):
            return 'quantity_step_invalid'
        if 'limit_up' in params and 'limit_down' in params:
            upper = self._decimal(params['limit_up'], 'limit_up', positive=True)
            lower = self._decimal(params['limit_down'], 'limit_down', positive=True)
            if price is not None and not lower <= Decimal(str(price)) <= upper:
                return 'outside_limit_price'
        if order.side is Side.SELL and quantity is not None and quantity > sellable.get(order.symbol, 0):
            return 't_plus_one_locked'
        return None


# --- corporate-action normalization ---
"""Pure normalization of corporate-action facts and as-of adjustment signals."""


class InvalidCorporateAction(ValueError):
    pass


def _date(value, field: str, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise InvalidCorporateAction(f"missing required date: {field}")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise InvalidCorporateAction(f"invalid {field}: {value!r}") from exc


_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _number(value, field: str, default: float = 0.0) -> float:
    if value in (None, ""):
        if math.isnan(default):
            raise InvalidCorporateAction(f"missing numeric {field}")
        return default
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        raise InvalidCorporateAction(f"non-numeric {field}: {value!r}")
    try:
        result = float(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidCorporateAction(f"non-numeric {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise InvalidCorporateAction(f"non-finite {field}: {value!r}")
    return result


@dataclass(frozen=True)
class CorporateActionEvent:
    code: str
    registration_date: date | None
    operation_date: date
    payment_date: date | None
    stock_market_date: date | None
    cash_per_share: float
    stock_dividend_per_share: float
    reserve_to_stock_per_share: float
    share_ratio: float
    cash_after_tax_text: str | None
    executable: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class DistributionEntitlement:
    event: CorporateActionEvent
    registration_shares: int
    gross_cash: Decimal
    bonus_shares: Decimal
    reserve_shares: Decimal
    cash_due: date | None
    shares_due: date | None
    tax_verified: bool
    requires_fractional_allocation: bool


def calculate_entitlement(event: CorporateActionEvent, snapshot_date: date,
                          shares: int) -> DistributionEntitlement:
    """Calculate gross claims from a registration-close snapshot, not live holdings.

    No posting, rounding, tax estimation or reinvestment occurs here. Fractional
    shares remain explicit until an independently specified allocation rule.
    """
    if not event.executable or snapshot_date != event.registration_date:
        raise InvalidCorporateAction('valid registration-close snapshot required')
    if isinstance(shares, bool) or not isinstance(shares, Integral) or shares < 0:
        raise InvalidCorporateAction('registration shares must be nonnegative integers')
    count = Decimal(int(shares))
    cash = count * Decimal(str(event.cash_per_share))
    bonus = count * Decimal(str(event.stock_dividend_per_share))
    reserve = count * Decimal(str(event.reserve_to_stock_per_share))
    fractional = any(value != value.to_integral_value() for value in (bonus, reserve))
    return DistributionEntitlement(event, int(shares), cash, bonus, reserve,
                                   event.payment_date, event.stock_market_date,
                                   False, fractional)


@dataclass(frozen=True)
class AdjustmentSignal:
    code: str
    as_of: date
    fore_factor: float
    back_factor: float
    coverage_start: date
    coverage_end: date
    source_operation_date: date | None
    anchor_source: str


@dataclass(frozen=True)
class ReconciledAction:
    security_id: str
    distribution_id: str
    event: CorporateActionEvent
    observations: tuple[tuple[str, CorporateActionEvent], ...]


def reconcile_corporate_actions(observations, identities) -> tuple[ReconciledAction, ...]:
    """Coalesce identical gross terms by explicit security and distribution identity.

    Observations are (source, distribution_id, event). The caller must establish
    each distribution identity from source evidence, never row index or amount.
    Distinct plans can pay on the same day; they remain separate entitlements.
    Preserve all source observations, including provider tax text. Conflicting
    terms within one plan fail; no provider selection or amount summation occurs.
    The caller owns source hashes and identity evidence; no identity fallback.
    """
    groups = {}
    for source, distribution_id, event in observations:
        security_id = identities[event.code]
        if not source or not security_id or not isinstance(distribution_id, str) or not distribution_id.strip():
            raise InvalidCorporateAction('source, security and distribution identity required')
        canonical = replace(event, code=security_id, cash_after_tax_text=None)
        key = (security_id, distribution_id)
        if key not in groups:
            groups[key] = (canonical, {})
        previous, evidence = groups[key]
        if previous != canonical:
            raise InvalidCorporateAction(f'conflicting corporate action terms: {key}')
        if source in evidence and evidence[source] != event:
            raise InvalidCorporateAction(f'conflicting source observation: {source}')
        evidence[source] = event
    return tuple(ReconciledAction(key[0], key[1], event, tuple(sorted(evidence.items())))
                 for key, (event, evidence) in sorted(groups.items()))


def normalize_dividend_records(records) -> tuple[CorporateActionEvent, ...]:
    """Normalize gross terms, not investor taxes.

    ``executable`` checks required settlement dates only; it does not certify
    after-tax ledger readiness. Every event retains ``tax_unverified``.
    Cash never contributes to ``share_ratio``.
    """
    out = []
    seen = set()
    for record in records:
        code = str(record.get("code", "")).strip()
        if not code:
            raise InvalidCorporateAction("missing code")
        operation = _date(record.get("operation_date"), "operation_date", True)
        key = (code, operation)
        if key in seen:
            raise InvalidCorporateAction(f"duplicate corporate action: {code} {operation}")
        seen.add(key)
        after_tax = record.get("cash_after_tax_text")
        cash = _number(record.get("cash_per_share"), "cash_per_share", default=float("nan"))
        stock = _number(record.get("stock_dividend_per_share"), "stock_dividend_per_share", default=float("nan"))
        reserve = _number(record.get("reserve_to_stock_per_share"), "reserve_to_stock_per_share", default=float("nan"))
        if min(cash, stock, reserve) < 0:
            raise InvalidCorporateAction(f"negative corporate action value: {key}")
        registration = _date(record.get("registration_date"), "registration_date")
        payment = _date(record.get("payment_date"), "payment_date")
        market = _date(record.get("stock_market_date"), "stock_market_date")
        if cash == 0 and stock == 0 and reserve == 0:
            raise InvalidCorporateAction(f"no economic terms: {key}")
        issues = ["tax_unverified"]
        if registration is None:
            issues.append("registration_date")
        elif registration >= operation:
            issues.append("registration_not_before_operation")
        if cash > 0 and payment is None:
            issues.append("payment_date")
        elif cash > 0 and payment < operation:
            issues.append("payment_before_operation")
        if stock + reserve > 0 and market is None:
            issues.append("stock_market_date")
        elif stock + reserve > 0 and market < operation:
            issues.append("stock_market_before_operation")
        issues = tuple(issues)
        blocking = tuple(issue for issue in issues if issue != "tax_unverified")
        out.append(CorporateActionEvent(
            code=code, registration_date=registration, operation_date=operation,
            payment_date=payment, stock_market_date=market,
            cash_per_share=cash, stock_dividend_per_share=stock,
            reserve_to_stock_per_share=reserve,
            share_ratio=1.0 + stock + reserve,
            cash_after_tax_text=None if after_tax in (None, "") else str(after_tax),
            executable=not blocking, issues=issues))
    return tuple(out)


def adjustment_factor_asof(code: str, records, as_of: date, coverage_start: date,
                           coverage_end: date, anchor_fore: float,
                           anchor_back: float) -> AdjustmentSignal:
    """Return the last factor effective on ``as_of``; no implicit factor-one anchor."""
    if not code or coverage_start is None or coverage_end is None or anchor_fore is None or anchor_back is None:
        raise InvalidCorporateAction("factor coverage and initial anchors are required")
    if coverage_start > coverage_end or not (coverage_start <= as_of <= coverage_end):
        raise InvalidCorporateAction("as_of is outside factor coverage")
    fore = _number(anchor_fore, "anchor_fore", default=float("nan"))
    back = _number(anchor_back, "anchor_back", default=float("nan"))
    if fore <= 0 or back <= 0:
        raise InvalidCorporateAction("factor anchors must be positive")
    selected = []
    seen = set()
    for record in records:
        record_code = str(record.get("code", "")).strip()
        if not record_code or record_code != code:
            raise InvalidCorporateAction("factor records must have one explicit matching code")
        operation = _date(record.get("date"), "date", True)
        key = (record_code, operation)
        if key in seen:
            raise InvalidCorporateAction(f"duplicate factor event: {key}")
        seen.add(key)
        if not (coverage_start <= operation <= coverage_end):
            raise InvalidCorporateAction(f"factor event outside coverage: {operation}")
        record_fore = _number(record.get("forward"), "forward", default=float("nan"))
        record_back = _number(record.get("backward"), "backward", default=float("nan"))
        if record_fore <= 0 or record_back <= 0:
            raise InvalidCorporateAction("adjustment factors must be positive")
        if operation <= as_of:
            selected.append((operation, record_fore, record_back))
    if selected:
        source, fore, back = sorted(selected)[-1]
    else:
        source = None
    return AdjustmentSignal(code=code, as_of=as_of, fore_factor=fore,
                            back_factor=back, coverage_start=coverage_start,
                            coverage_end=coverage_end,
                            source_operation_date=source,
                            anchor_source="explicit_input")


# --- distribution postings ---
"""Source-bound gross distributions into the sole authoritative account.

Delivery quantities/dates and originating lots must be supplied explicitly.
No adjustment-factor shares, fractional allocation, taxes, source lookup or
price inference. Gross claims are not a tax-complete investor NAV: the caller
must supply independently validated tax assessments and liabilities.
"""



@dataclass(frozen=True)
class ShareDelivery:
    originating_lot: str
    acquired: date
    shares: int
    credit_date: date
    tradable_from: date


@dataclass(frozen=True)
class DistributionPosting:
    distribution_id: str
    symbol: str
    due_date: date
    processed_as_of: date
    kind: str
    quantity: Decimal
    source_sha256: str
    acquired: date | None = None
    event_id: str | None = None


class AccountDistributionLedger:
    def __init__(self, account: AccountLedger):
        self.account=account
        self._claims={}; self._posted=set(); self._as_of=None

    def register(self, distribution_id: str, claim: DistributionEntitlement,
                 source_sha256: str, deliveries: tuple[ShareDelivery,...]):
        key=(claim.event.code,distribution_id)
        payload=(claim,source_sha256,deliveries)
        if key in self._claims:
            if self._claims[key]!=payload: raise ValueError('conflicting distribution replay')
            return
        event=claim.event
        if (not isinstance(distribution_id,str) or not distribution_id.strip()
                or not isinstance(source_sha256,str) or not re.fullmatch('[0-9a-f]{64}',source_sha256)
                or not event.executable or claim.requires_fractional_allocation
                or event.registration_date!=self.account.as_of
                or (self._as_of is not None and event.operation_date<=self._as_of)):
            raise ValueError('sourced identified nonfractional registration-close entitlement required')
        lots={lot.lot_id:lot for lot in self.account.lots if lot.symbol==event.code}
        if sum(lot.shares for lot in lots.values())!=claim.registration_shares:
            raise ValueError('registration entitlement differs from actual account snapshot')
        expected=calculate_entitlement(event,event.registration_date,claim.registration_shares)
        if any(getattr(claim,field)!=getattr(expected,field) for field in
               ('gross_cash','bonus_shares','reserve_shares','cash_due','shares_due')):
            raise ValueError('entitlement monetary terms differ from sourced distribution')
        total=claim.bonus_shares+claim.reserve_shares
        if (any(not isinstance(n,Decimal) or not n.is_finite() or n<0
                for n in (claim.gross_cash,claim.bonus_shares,claim.reserve_shares))
                or total!=total.to_integral_value()
                or (claim.gross_cash>0 and (claim.cash_due is None or claim.cash_due<event.operation_date))
                or not isinstance(deliveries,tuple)
                or sum(d.shares for d in deliveries)!=total):
            raise ValueError('complete actual delivery quantities and payment date required')
        identities=set()
        for delivery in deliveries:
            identity=(delivery.originating_lot,delivery.credit_date)
            if (delivery.originating_lot not in lots or identity in identities
                    or type(delivery.shares) is not int or delivery.shares<=0
                    or delivery.acquired!=lots[delivery.originating_lot].acquired
                    or not isinstance(delivery.credit_date,date)
                    or delivery.credit_date<event.operation_date
                    or not isinstance(delivery.tradable_from,date)
                    or delivery.tradable_from<delivery.credit_date
                    or delivery.tradable_from!=event.stock_market_date):
                raise ValueError('source-bound originating lot and distinct credit/tradable dates required')
            identities.add(identity)
        self._claims[key]=payload

    def due_dates(self, as_of):
        dates=set()
        for key,(claim,_,deliveries) in self._claims.items():
            if claim.gross_cash and (*key,'cash',0) not in self._posted and claim.cash_due<=as_of:
                dates.add(claim.cash_due)
            for index,delivery in enumerate(deliveries):
                if (*key,'shares',index) not in self._posted and delivery.credit_date<=as_of:
                    dates.add(delivery.credit_date)
        return tuple(sorted(dates))

    def advance(self, as_of: date) -> tuple[DistributionPosting,...]:
        if not isinstance(as_of,date) or (self._as_of is not None and as_of<self._as_of):
            raise ValueError('distribution time cannot move backwards')
        due=[]
        for key,(claim,source,deliveries) in self._claims.items():
            if claim.gross_cash>0 and (*key,'cash',0) not in self._posted and claim.cash_due<=as_of:
                due.append((claim.cash_due,key,'cash',0,claim.gross_cash,None,source))
            for index,delivery in enumerate(deliveries):
                if (*key,'shares',index) not in self._posted and delivery.credit_date<=as_of:
                    due.append((delivery.credit_date,key,'shares',index,Decimal(delivery.shares),delivery,source))
        due.sort(key=lambda row:row[:4])
        if any(self.account.as_of is not None and row[0]<self.account.as_of for row in due):
            raise ValueError('missed settlement date; cannot backdate account postings')
        postings=[]
        for when,key,kind,index,quantity,delivery,source in due:
            event_id=sha256(repr(('distribution',key,kind,index)).encode()).hexdigest()
            if kind=='cash':
                self.account.record_cash(event_id,when,'dividend',quantity,source)
            else:
                self.account.record_share_credit(event_id,when,key[0],int(quantity),delivery.acquired,
                                                 source,tradable_from=delivery.tradable_from)
            self._posted.add((*key,kind,index))
            postings.append(DistributionPosting(key[1],key[0],when,as_of,kind,quantity,source,None if delivery is None else delivery.acquired,event_id))
        self._as_of=as_of
        return tuple(postings)

    def receivable_value(self, as_of: date, raw_marks) -> Decimal:
        if as_of!=self._as_of:
            raise ValueError('claims require current distribution ledger date')
        total=Decimal(0)
        for key,(claim,source,deliveries) in self._claims.items():
            if claim.event.operation_date>as_of: continue
            if (*key,'cash',0) not in self._posted: total+=claim.gross_cash
            pending=sum(d.shares for index,d in enumerate(deliveries)
                        if (*key,'shares',index) not in self._posted)
            if pending:
                if key[0] not in raw_marks: raise ValueError('share claim lacks explicit raw mark')
                price=Decimal(str(raw_marks[key[0]]))
                if not price.is_finite() or price<0: raise ValueError('finite nonnegative claim mark required')
                total+=pending*price
        return total


# --- dividend tax mathematics ---
"""Pure SH/SZ unrestricted-individual dividend tax calculations.

Sources:
https://shanghai.chinatax.gov.cn/zcfw/zcjd/201211/t400944.html
https://www.chinatax.gov.cn/n810341/n810755/c1797427/content.html

Taxable income is supplied explicitly: cash, taxable stock-dividend nominal
value and reserve-capital classifications must come from action evidence.
This module does not infer them from a factor or a share ratio. No cent
rounding or broker deduction date is assumed here. It is not a complete tax
ledger; settlement and daily net share changes remain the caller's duties.
"""
@dataclass(frozen=True)
class TaxResult:
    taxable_income: Decimal
    rate: Decimal
    withheld: Decimal
    final_tax: Decimal
    supplement: Decimal
    policy: str


@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    acquired: date
    shares: int


# --- tax ledger ---
"""Actual-fill dividend tax ledger for a declared unrestricted-individual policy.

Tax lots use daily net share changes and FIFO. An unsold right is a potential
NAV liability, never a cash debit. Disposal fixes the tax. The declared research
settlement date is the later of disposal and income availability, not a claim
about a broker's real deduction timestamp. Unsupported share-tax policy fails.
"""


def money(value):
    number=Decimal(str(value))
    if not number.is_finite() or number<0: raise ValueError('finite nonnegative tax amount required')
    return number


@dataclass(frozen=True)
class TaxPolicy:
    effective: date
    policy: str
    source_sha256: str
    bonus_holding_period: str | None
    bonus_tax_allocation: str | None

    @classmethod
    def from_mapping(cls,raw,source_sha256):
        required={'effective','policy','payment_timing','liability','rounding'}
        if required-set(raw) or set(raw)-required-{'bonus_holding_period','bonus_tax_allocation'}:
            raise ValueError('tax policy fields missing or unsupported')
        starts={'财税2015-101':date(2015,9,9),
                '财税2012-85+财税2015-101':date(2013,1,1)}
        if (raw['policy'] not in starts or date.fromisoformat(raw['effective'])<starts[raw['policy']]
                or raw['payment_timing']!='later_of_disposal_and_income'
                or raw['liability']!='current_holding_period' or raw['rounding']!='none'
                or not re.fullmatch('[0-9a-f]{64}',source_sha256)):
            raise ValueError('unsupported dividend tax policy or source')
        return cls(date.fromisoformat(raw['effective']),raw['policy'],source_sha256,
                   raw.get('bonus_holding_period'),raw.get('bonus_tax_allocation'))

    def terms(self, registration):
        if not isinstance(registration,date) or registration<date(2013,1,1):
            raise ValueError('unsupported dividend record date')
        if registration<=date(2015,9,8):
            return Decimal('.05'),Decimal('.05'),'财税2012-85'
        return Decimal(0),Decimal(0),'财税2015-101'

@dataclass(frozen=True)
class TaxRight:
    registration: date
    distribution_id: str
    component: str
    operation: date
    due: date
    income_per_share: Decimal
    source_sha256: str


@dataclass(frozen=True)
class TaxHolding:
    lot_id: str
    symbol: str
    acquired: date
    shares: int
    rights: tuple[TaxRight,...]=()


@dataclass(frozen=True)
class TaxPayment:
    event_id: str
    distribution_id: str
    symbol: str
    acquired: date
    disposed: date
    operation: date
    due: date
    amount: Decimal
    source_sha256: str
    policy: str


class TaxLedger:
    def __init__(self,policy:TaxPolicy):
        self.policy=policy; self.lots=(); self.pending={}; self.payments=[]
        self._day=None; self._opening=None; self._registrations={}; self._credits={}
        self.deliveries=[]

    def begin(self,day):
        if not isinstance(day,date) or (self._day is not None and day<=self._day):
            raise ValueError('tax sessions must move forward exactly once')
        self._day=day; self._opening=(self.lots,dict(self.pending))

    def register(self,distribution_id,event,stock_nominal,reserve_taxable,source_sha256,*,credit_date=None):
        if (event.registration_date is None or event.registration_date<self.policy.effective
                or not event.executable or not distribution_id or not re.fullmatch('[0-9a-f]{64}',source_sha256)):
            raise ValueError('unsupported or unsourced registration tax facts')
        stock_nominal,reserve_taxable=money(stock_nominal),money(reserve_taxable)
        bonus=Decimal(str(event.stock_dividend_per_share))+Decimal(str(event.reserve_to_stock_per_share))
        if bonus:
            if (self.policy.bonus_holding_period!='originating_lot'
                    or self.policy.bonus_tax_allocation!='proportional_entitlement'
                    or not isinstance(credit_date,date)
                    or not event.operation_date<=credit_date<=event.stock_market_date):
                raise ValueError('explicit sourced bonus holding-period/allocation policy required')
        elif reserve_taxable: raise ValueError('taxable reserve without a reserve share event')
        payload=(event,stock_nominal,reserve_taxable,source_sha256,credit_date)
        key=(event.code,distribution_id)
        if key in self._registrations:
            if self._registrations[key]!=payload: raise ValueError('conflicting tax registration')
            return
        rights=[]
        cash=Decimal(str(event.cash_per_share))
        shares=Decimal(str(event.stock_dividend_per_share))*stock_nominal+reserve_taxable
        if cash: rights.append(TaxRight(event.registration_date,distribution_id,'cash',event.operation_date,event.payment_date,cash,source_sha256))
        if shares: rights.append(TaxRight(event.registration_date,distribution_id,'shares',event.operation_date,credit_date,shares,source_sha256))
        _,withholding_rate,policy=self.policy.terms(event.registration_date)
        if withholding_rate:
            for lot in self.lots:
                if lot.symbol!=event.code:
                    continue
                for right in rights:
                    identity=canonical_hash((distribution_id,right.component,lot.lot_id,'withholding'))
                    self.pending[identity]=TaxPayment(identity,distribution_id,event.code,lot.acquired,
                        event.registration_date,right.operation,right.due,
                        right.income_per_share*lot.shares*withholding_rate,source_sha256,policy)
        self.lots=tuple(replace(lot,rights=lot.rights+tuple(rights)) if lot.symbol==event.code else lot for lot in self.lots)
        if bonus:
            for lot in self.lots:
                if lot.symbol!=event.code: continue
                count=lot.shares*bonus
                if count!=count.to_integral_value(): raise ValueError('fractional tax bonus allocation requires source evidence')
                if count:
                    identity=canonical_hash((event.code,distribution_id,lot.lot_id,'tax_bonus'))
                    self.deliveries.append((credit_date,identity,event.code,lot.acquired,int(count)))
        self._registrations[key]=payload

    def credit(self,identity,symbol,acquired,shares,day):
        payload=(symbol,acquired,shares,day)
        if identity in self._credits:
            if self._credits[identity]!=payload: raise ValueError('conflicting tax share credit')
            return
        if (type(shares) is not int or shares<=0 or acquired>day
                or self.policy.bonus_holding_period!='originating_lot'
                or self.policy.bonus_tax_allocation!='proportional_entitlement'):
            raise ValueError('unsupported share-tax credit facts')
        existing=tuple(lot for lot in self.lots if lot.symbol==symbol and lot.acquired==acquired)
        count=sum(lot.shares for lot in existing); total=count+shares
        grouped={}
        for lot in existing:
            for right in lot.rights:
                key=(right.registration,right.distribution_id,right.component,right.operation,right.due,right.source_sha256)
                grouped[key]=grouped.get(key,Decimal(0))+right.income_per_share*lot.shares
        bonus_rights=tuple(TaxRight(*key[:5],amount/total,key[5]) for key,amount in grouped.items())
        factor=Decimal(count)/total
        self.lots=tuple(replace(lot,rights=tuple(replace(right,income_per_share=right.income_per_share*factor) for right in lot.rights))
            if lot in existing else lot for lot in self.lots)+(TaxHolding(identity,symbol,acquired,shares,bonus_rights),)
        self._credits[identity]=payload

    def advance(self,as_of):
        for when,identity,symbol,acquired,shares in sorted(self.deliveries):
            if when<=as_of: self.credit(identity,symbol,acquired,shares,when)

    def reserve(self,as_of):
        return sum((payment.amount for payment in self.pending.values() if payment.operation<=as_of),Decimal(0))

    def settle(self,as_of):
        due=tuple(payment for payment in self.pending.values() if payment.operation<=as_of and payment.due<=as_of)
        for payment in due: del self.pending[payment.event_id]
        self.payments.extend(due)
        return due


# --- adjusted signal value types ---


@dataclass(frozen=True)
class CoverageIssue:
    code: str
    reason: str
    detail: str


class AdjustmentCoverageError(ValueError):
    """A source-integrity or date-coverage gate failed before panel release."""

    def __init__(self, issues: tuple[CoverageIssue, ...], *, checked_bars: int | None = None):
        self.issues = issues
        self.checked_bars = checked_bars
        preview = "; ".join(f"{issue.code}:{issue.reason} ({issue.detail})"
                            for issue in issues[:5])
        suffix = f"; and {len(issues) - 5} more" if len(issues) > 5 else ""
        super().__init__(f"adjustment coverage rejected: {preview}{suffix}")


@dataclass(frozen=True)
class SourceHash:
    path: str
    sha256: str


@dataclass(frozen=True)
class FactorEvent:
    code: str
    effective_date: date
    fore_factor: float
    back_factor: float


@dataclass(frozen=True)
class ListingAnchorEvidence:
    code: str
    ipo_date: date
    source_path: str
    raw_response_sha256: str


@dataclass(frozen=True)
class AdjustedPricePanel:
    """Raw execution bars and independent, non-investable signal price index.

    ``back_factor`` and ``signal_*`` are for indicators only. They do not
    encode cash receivable, payable tax, or shares owed by a distribution.
    All arrays have shape ``(len(symbols), len(calendar))``; absent raw bars
    and their signal values are NaN.
    """

    calendar: tuple[date, ...]
    symbols: tuple[str, ...]
    names: tuple[str, ...]
    raw_open: np.ndarray
    raw_high: np.ndarray
    raw_low: np.ndarray
    raw_close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    back_factor: np.ndarray
    traded_bar: np.ndarray
    signal_close: np.ndarray
    signal_high: np.ndarray
    factor_events: tuple[FactorEvent, ...]
    listing_anchors: tuple[ListingAnchorEvidence, ...]
    source_hashes: tuple[SourceHash, ...]


# --- own-trading-day rolling features ---
"""Own-trading-day rolling features for a wide (symbol x calendar-day) panel.

Why this module exists
----------------------
A naive ``np.cumsum``-based rolling mean is WRONG for an A-share panel:

  * 58% of the 5,162 symbols have no data on the first calendar column
    (they IPO'd after 2014-01-02), and 2,921 symbols carry interior
    suspension gaps (266,435 NaN days in total).
  * ``np.cumsum`` propagates a single NaN to every later element, so the
    prefix-sum difference is NaN for the entire remaining history.

Consequence measured on the real panel: a cumsum MA120 was entirely NaN for
3,700 / 5,162 symbols, ``close > ma120`` was therefore ``False`` for 71.9% of
symbols over their whole life, and market breadth collapsed to a 5.7% mean
(never reaching the strategy's 60% entry gate). See tests/test_panel_features.py.

Semantics implemented here
--------------------------
Every window is measured over the symbol's OWN valid trading days, which is
how MA120 / 20-day return are defined on every trading platform: a suspension
(停牌) day is not a bar, it is skipped, not treated as a zero or as a gap.
Results are scattered back onto the shared calendar so that column ``t`` always
means the same date for every symbol.
"""

__all__ = [
    "Calculation", "AllocationLimits", "AllocationDecision", "AllocationPlan",
    "AccountExecutionSession", "PositionLot", "CashPosting",
    "OrderCancellation", "AccountPoint", "AccountLedger", "TradeRules", "FeeBreakdown",
    "OpeningRiskSnapshot", "FeePolicy", "InvalidCorporateAction",
    "CorporateActionEvent", "DistributionEntitlement", "calculate_entitlement",
    "AdjustmentSignal", "ReconciledAction", "reconcile_corporate_actions",
    "normalize_dividend_records", "adjustment_factor_asof", "ShareDelivery",
    "DistributionPosting", "AccountDistributionLedger", "TaxResult", "TaxLot", "TaxPolicy", "TaxRight", "TaxHolding",
    "TaxPayment", "TaxLedger", "CoverageIssue", "AdjustmentCoverageError", "SourceHash",
    "FactorEvent", "ListingAnchorEvidence", "AdjustedPricePanel",
]
