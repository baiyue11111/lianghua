"""Order/fill orchestration over one authoritative account; no I/O or strategy.

The existing execution model receives an ephemeral *execution capacity* book,
not a second portfolio. Only AccountLedger postings change persistent cash and
positions. Corporate-action claims and taxes must be settled by the caller;
this boundary does not certify prices, limits, fee rounding or source coverage.
"""
from datetime import date
from typing import Mapping

from account_ledger import AccountLedger
from protocal import Bar, Order, Side, Fill
from execution import ExecutionEngine, PositionBook, remaining_order
from account_allocation import AllocationLimits, allocate_target
from execution_risk import (OpeningRiskSnapshot, opening_equity, opening_rejection,
                             validate_opening_risk, nonnegative)


class AccountExecutionSession:
    def __init__(self, account: AccountLedger, fees):
        self.account=account
        self._engine=ExecutionEngine(fees)
        self._originals={}; self._pending={}; self._attempts={}; self._fills=[]
        self._targets={}
        self._open_attempts={}

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

    def _preview(self,order,bar,previous_close,reserve=0):
        available={}
        for lot in self.account.lots:
            if lot.tradable_from<=bar.trade_date:
                available[lot.symbol]=available.get(lot.symbol,0)+lot.shares
        # BUY pricing needs cash; SELL pricing needs only today's sellable
        # quantities. The engine's temporary average costs are never persisted.
        capacity=PositionBook(float(max(0,self.account.cash-reserve)),available)
        return self._engine.execute(order,bar,capacity,previous_close)

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
                     previous_closes: Mapping[str, float | None], *, risk: OpeningRiskSnapshot, cash_reserve=None, on_fill=None):
        """Execute a complete pending-order snapshot, sells before buys.

        Missing bars are not interpreted as suspensions. The data adapter must
        supply explicit observations, including zero-volume bars when proved.
        Validate every required observation before changing any account state.
        This is not an all-or-nothing trade transaction: ordinary execution
        rejections remain individually audited and pending.
        """
        inputs=(tuple(sorted(bars.items())),tuple(sorted(previous_closes.items())),risk.liquidity_as_of,
                tuple(sorted(risk.raw_marks.items())),tuple(sorted(risk.amount20.items())),
                risk.receivables,risk.liabilities,risk.limits)
        if trade_date in self._open_attempts:
            original,fills=self._open_attempts[trade_date]
            if original!=inputs: raise ValueError('conflicting opening replay')
            return fills
        orders=sorted(self.pending,key=lambda order:(order.side!=Side.SELL,order.order_id))
        holdings=self._holdings()
        validate_opening_risk(risk,trade_date,holdings,orders)
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
        fills=[]
        for order in orders:
            bar=bars[order.symbol]; previous=previous_closes[order.symbol]
            fill=self._preview(order,bar,previous,0 if cash_reserve is None else cash_reserve())
            reason=opening_rejection(order,fill,equity=opening_equity(self.account.cash,self._holdings(),risk),
                                     holdings=self._holdings(),snapshot=risk)
            if reason:
                fill=Fill(order.order_id,order.symbol,order.side,trade_date,0,0,0,0,0,'rejected',reason)
            fills.append(self._record_attempt(order,bar,fill,(bar,previous,inputs)))
            if on_fill is not None: on_fill(fill)
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
        plan=allocate_target(target,next_session=next_session,equity=equity,
                             holdings=holdings,raw_closes=raw_closes,amount20=amount20,limits=limits,
                             rebalance=rebalance)
        if rebalance:
            for order in self.pending:
                self.cancel(order.order_id,target.as_of.date(),target.reasons.get('__market__','target_replaced'))
            for order in plan.orders: self.submit(order)
        self._targets[key]=(inputs,plan)
        return plan
