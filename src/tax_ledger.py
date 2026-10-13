"""Actual-fill dividend tax ledger for a declared unrestricted-individual policy.

Tax lots use daily net share changes and FIFO. An unsold right is a potential
NAV liability, never a cash debit. Disposal fixes the tax. The declared research
settlement date is the later of disposal and income availability, not a claim
about a broker's real deduction timestamp. Unsupported share-tax policy fails.
"""
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import re
from protocal import Side
from dividend_tax import holding_rate
from strategy import canonical_hash


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
        if (raw['policy']!='财税2015-101' or date.fromisoformat(raw['effective'])<date(2015,9,9)
                or raw['payment_timing']!='later_of_disposal_and_income'
                or raw['liability']!='current_holding_period' or raw['rounding']!='none'
                or not re.fullmatch('[0-9a-f]{64}',source_sha256)):
            raise ValueError('unsupported dividend tax policy or source')
        return cls(date.fromisoformat(raw['effective']),raw['policy'],source_sha256,
                   raw.get('bonus_holding_period'),raw.get('bonus_tax_allocation'))

    def rate(self,acquired,disposed):
        if disposed<acquired or disposed<self.effective:
            raise ValueError('date is outside declared dividend tax policy')
        return holding_rate(acquired,disposed,Decimal(0))


@dataclass(frozen=True)
class TaxRight:
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

    def _sell(self,symbol,shares,day):
        if type(shares) is not int or shares<0 or sum(l.shares for l in self.lots if l.symbol==symbol)<shares:
            raise ValueError('tax disposal exceeds actual tax holdings')
        remaining=[]; needed=shares
        for lot in sorted(self.lots,key=lambda lot:lot.acquired):
            take=min(needed,lot.shares) if lot.symbol==symbol else 0
            needed-=take
            for right in lot.rights:
                tax=right.income_per_share*take*self.policy.rate(lot.acquired,day)
                if tax:
                    key=canonical_hash((right.distribution_id,right.component,lot.lot_id,day.isoformat()))
                    old=self.pending[key].amount if key in self.pending else Decimal(0)
                    self.pending[key]=TaxPayment(key,right.distribution_id,symbol,lot.acquired,day,right.operation,
                        max(day,right.due),old+tax,right.source_sha256,self.policy.policy)
            if take<lot.shares: remaining.append(replace(lot,shares=lot.shares-take))
        self.lots=tuple(remaining)

    def preview(self,fill):
        if self._opening is None or fill.trade_date!=self._day: raise ValueError('tax preview outside current session')
        if fill.status=='filled' and fill.side is Side.SELL:
            self._sell(fill.symbol,fill.shares,fill.trade_date)

    def end(self,fills):
        if self._opening is None or any(fill.trade_date!=self._day for fill in fills):
            raise ValueError('tax daily net settlement requires current fills')
        self.lots,self.pending=self._opening; self.pending=dict(self.pending)
        symbols=sorted({fill.symbol for fill in fills if fill.status=='filled'})
        for symbol in symbols:
            buys=sum(fill.shares for fill in fills if fill.symbol==symbol and fill.status=='filled' and fill.side is Side.BUY)
            sells=sum(fill.shares for fill in fills if fill.symbol==symbol and fill.status=='filled' and fill.side is Side.SELL)
            if sells>buys: self._sell(symbol,sells-buys,self._day)
            elif buys>sells:
                identity=canonical_hash((symbol,self._day.isoformat(),tuple(fill.order_id for fill in fills if fill.symbol==symbol)))
                self.lots+= (TaxHolding(identity,symbol,self._day,buys-sells),)
        self._opening=None

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
        if cash: rights.append(TaxRight(distribution_id,'cash',event.operation_date,event.payment_date,cash,source_sha256))
        if shares: rights.append(TaxRight(distribution_id,'shares',event.operation_date,credit_date,shares,source_sha256))
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
                key=(right.distribution_id,right.component,right.operation,right.due,right.source_sha256)
                grouped[key]=grouped.get(key,Decimal(0))+right.income_per_share*lot.shares
        bonus_rights=tuple(TaxRight(*key[:4],amount/total,key[4]) for key,amount in grouped.items())
        factor=Decimal(count)/total
        self.lots=tuple(replace(lot,rights=tuple(replace(right,income_per_share=right.income_per_share*factor) for right in lot.rights))
            if lot in existing else lot for lot in self.lots)+(TaxHolding(identity,symbol,acquired,shares,bonus_rights),)
        self._credits[identity]=payload

    def advance(self,as_of):
        for when,identity,symbol,acquired,shares in sorted(self.deliveries):
            if when<=as_of: self.credit(identity,symbol,acquired,shares,when)

    def reserve(self,as_of):
        return sum((payment.amount for payment in self.pending.values() if payment.operation<=as_of),Decimal(0))

    def liability(self,as_of):
        potential=sum((lot.shares*right.income_per_share*self.policy.rate(lot.acquired,as_of)
            for lot in self.lots for right in lot.rights if right.operation<=as_of),Decimal(0))
        return potential+self.reserve(as_of)

    def settle(self,as_of):
        due=tuple(payment for payment in self.pending.values() if payment.operation<=as_of and payment.due<=as_of)
        for payment in due: del self.pending[payment.event_id]
        self.payments.extend(due)
        return due
