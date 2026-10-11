"""Source-bound gross distributions into the sole authoritative account.

Delivery quantities/dates and originating lots must be supplied explicitly.
No adjustment-factor shares, fractional allocation, taxes, source lookup or
price inference. Gross claims are not a tax-complete investor NAV: the caller
must supply independently validated tax assessments and liabilities.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
import re

from account_ledger import AccountLedger
from corporate_actions import DistributionEntitlement, calculate_entitlement


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
