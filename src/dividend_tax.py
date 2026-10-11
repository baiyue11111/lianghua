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
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class TaxResult:
    taxable_income: Decimal
    rate: Decimal
    withheld: Decimal
    final_tax: Decimal
    supplement: Decimal
    policy: str


def _anniversary(acquired: date, months: int) -> date:
    ordinal=acquired.year*12+acquired.month-1+months
    year,month=divmod(ordinal,12); month+=1
    return date(year,month,min(acquired.day,monthrange(year,month)[1]))


def holding_rate(acquired: date, disposed: date, long_rate: Decimal) -> Decimal:
    """One natural-month/year rule, with a policy-supplied long-term rate."""
    if (not isinstance(acquired,date) or not isinstance(disposed,date) or disposed<acquired
            or not isinstance(long_rate,Decimal) or not long_rate.is_finite() or long_rate<0):
        raise ValueError('valid holding dates and declared long-term tax rate required')
    if disposed<=_anniversary(acquired,1): return Decimal('.20')
    if disposed<=_anniversary(acquired,12): return Decimal('.10')
    return long_rate


def dividend_tax(acquired: date, disposed: date, registration: date,
                 taxable_income: Decimal) -> TaxResult:
    if (not all(isinstance(d,date) for d in (acquired,disposed,registration))
            or not acquired<=registration<disposed or registration<=date(2013,1,1)):
        raise ValueError('valid acquisition, record-close and disposal dates required; pre2013 unsupported')
    if not isinstance(taxable_income,Decimal) or not taxable_income.is_finite() or taxable_income<0:
        raise ValueError('finite nonnegative Decimal taxable income required')
    # 101号 expressly applies to registration dates after 2015-09-08.
    modern=registration>date(2015,9,8)
    rate=holding_rate(acquired,disposed,Decimal(0) if modern else Decimal('.05'))
    withheld=Decimal('0') if modern else taxable_income*Decimal('0.05')
    final=taxable_income*rate
    return TaxResult(taxable_income,rate,withheld,final,final-withheld,
                     '财税2015-101' if modern else '财税2012-85')


@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    acquired: date
    shares: int


def allocate_fifo(lots: tuple[TaxLot,...], net_disposed_shares: int,
                  disposed: date) -> tuple[tuple[TaxLot,...],tuple[TaxLot,...]]:
    """Allocate one security's end-of-day net disposal, not every sell fill.

    The tax authority uses daily net increases/decreases; intraday purchases
    and sales cannot be independently fed here as if they were net changes.
    IDs and dates are preserved through partial sales and code migration.
    """
    if (type(net_disposed_shares) is not int or net_disposed_shares<0
            or not isinstance(disposed,date)):
        raise ValueError('nonnegative integer net disposal and date required')
    if len({lot.lot_id for lot in lots})!=len(lots):
        raise ValueError('duplicate tax lot identity')
    if any(not lot.lot_id or type(lot.shares) is not int or lot.shares<=0
           or not isinstance(lot.acquired,date) or lot.acquired>disposed for lot in lots):
        raise ValueError('invalid tax lots')
    if sum(lot.shares for lot in lots)<net_disposed_shares:
        raise ValueError('net disposal exceeds holdings')
    indexed=sorted(enumerate(lots),key=lambda item:(item[1].acquired,item[0]))
    allocated,remaining=[],[]; needed=net_disposed_shares
    for _,lot in indexed:
        take=min(needed,lot.shares)
        if take: allocated.append(replace(lot,shares=take))
        if take<lot.shares: remaining.append(replace(lot,shares=lot.shares-take))
        needed-=take
    return tuple(allocated),tuple(remaining)
