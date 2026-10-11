"""Exact event account; no prices, signals, tax inference, execution or I/O.

Receivable/payable valuations must be supplied explicitly at every mark.
Corporate-action entitlements and taxes must be validated upstream. This
component alone does not certify corporate-action completeness or NAV.
"""
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
import re

from contracts import Fill, Order, Side


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
        if (sum((p.cash_delta for p in self._postings),Decimal(0))+self.initial_cash!=self._cash
                or abs(point.equity-self.initial_cash-self._realized-(value-cost)-self._income-receivables+liabilities)>Decimal('1e-18')):
            raise ValueError('account conservation invariant failed')
        self._day=day
        return point
