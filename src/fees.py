"""Explicit dated fee policy; dates outside supplied schedules are rejected."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from protocal import Side

@dataclass(frozen=True)
class FeePolicy:
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_rates: tuple
    transfer_rates: tuple
    source_sha256: str

    @classmethod
    def from_mapping(cls, raw, source_sha256):
        if raw['rounding'] != 'none' or not re.fullmatch('[0-9a-f]{64}',source_sha256):
            raise ValueError('explicit supported rounding and source hash required')
        def schedule(key):
            rows=tuple((date.fromisoformat(r['effective']),Decimal(r['rate'])) for r in raw[key])
            if not rows or tuple(sorted(set(d for d,_ in rows)))!=tuple(d for d,_ in rows):
                raise ValueError('ordered unique effective fee dates required')
            if any(not r.is_finite() or r<0 for _,r in rows): raise ValueError('invalid fee rate')
            return rows
        rate,minimum=Decimal(raw['commission_rate']),Decimal(raw['minimum_commission'])
        if any(not v.is_finite() or v<0 for v in (rate,minimum)): raise ValueError('invalid commission')
        return cls(rate,minimum,schedule('stamp_rates'),schedule('transfer_rates'),source_sha256)

    def calculate(self,gross,side,day):
        def rate(rows):
            eligible=[r for d,r in rows if d<=day]
            if not eligible: raise ValueError('fee effective date does not cover trade')
            return eligible[-1]
        stamp=rate(self.stamp_rates); transfer=rate(self.transfer_rates)
        return max(self.minimum_commission,gross*self.commission_rate), gross*stamp if side is Side.SELL else Decimal(0), gross*transfer
