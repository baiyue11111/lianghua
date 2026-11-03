"""Buffett-inspired quality/value screen, not a moat or intrinsic-value model.

Uses the latest PIT report, including cumulative (not annualized) ROE/EPS.
P/B is a valuation proxy, not estimated intrinsic value. Missing facts exclude
an instrument. No qualifying instruments means cash. Review quarterly and
trade only when the selected set changes. Intended for nonfinancial businesses.
Rank eligible businesses by equal-weight standardized net margin, low debt,
and book/price. These current-report proxies do not measure a durable moat.
quality_quarters measures consecutive quarter-end observations, not distinct
reports. Missing observations reset the window. Each run starts with no samples.
Margin uses the window minimum; debt uses its maximum; valuation remains current.
"""
from dataclasses import dataclass, field
from datetime import date
from math import isfinite

from core.calculation import Calculation
from protocol import DataRequest, MarketFrame, TargetSet
from strategy import validate_params

NAME = '巴菲特思路：质量与估值'


@dataclass(frozen=True)
class Params:
    quality_quarters: int = field(default=1, metadata={'label':'质量观察季度数','minimum':1,'maximum':20})
    positions: int = field(default=5, metadata={'label':'最多持股数','minimum':1,'maximum':100})
    margin_min: float = field(default=.1, metadata={'label':'最低净利率','minimum':0,'maximum':1})
    debt_max: float = field(default=.6, metadata={'label':'最高资产负债率','minimum':0,'maximum':1})
    pb_max: float = field(default=5., metadata={'label':'最高市净率','exclusive_minimum':0})
    exposure: float = field(default=1., metadata={'label':'总仓位','minimum':0,'maximum':1})

    def __post_init__(self):
        validate_params(self)


class Strategy:
    strategy_id = 'buffett'

    def __init__(self, params: Params):
        self.params = params
        self.selected = None
        self.observations = {}

    def requirements(self):
        return DataRequest(('close','book_value','roe','net_margin','debt_ratio','eps','cash_per_share'),1)

    def generate(self, frame, account):
        if type(frame.context['trading']) is not bool:
            raise ValueError('trading phase must be explicit')
        if not frame.context['trading']:
            return None
        following = date.fromisoformat(frame.context['next_session'])
        if following <= frame.at.date():
            raise ValueError('next_session must follow decision session')
        if (frame.at.year,(frame.at.month-1)//3) == (following.year,(following.month-1)//3):
            return None
        candidates = {}
        observations = {}
        p = self.params
        quarter = frame.at.year * 4 + (frame.at.month - 1) // 3
        fields = self.requirements().fields
        for symbol, row in frame.fields.items():
            values = [row[name] for name in fields]
            for value in values:
                if value is not None and (type(value) not in (int,float) or not isfinite(value)):
                    raise ValueError(f'{symbol}: fundamentals must be finite numbers or None')
            if any(value is None for value in values):
                continue
            close, book, roe, margin, debt, eps, cash = values
            if close <= 0:
                raise ValueError(f'{symbol}: close must be positive')
            previous = self.observations.get(symbol, ())
            if previous and previous[-1][0] == quarter:
                previous = previous[:-1]
            if previous and previous[-1][0] != quarter - 1:
                previous = ()
            samples = (*previous, (quarter,margin,debt))[-p.quality_quarters:]
            observations[symbol] = samples
            if len(samples) < p.quality_quarters:
                continue
            if any(m < p.margin_min or not 0 <= d <= p.debt_max for _,m,d in samples):
                continue
            if book <= 0 or roe <= 0 or eps <= 0 or cash <= 0:
                continue
            pb = close / book
            if pb <= p.pb_max:
                candidates[symbol] = {'net_margin':min(m for _,m,_ in samples),
                                      'debt_ratio':max(d for _,_,d in samples), 'book_yield':book / close}
        self.observations = observations
        scores = Calculation.fundamentals(
            MarketFrame(frame.at,frame.venue,candidates,frame.context),
            {'net_margin':1., 'debt_ratio':-1., 'book_yield':1.},
        ).fields if candidates else {}
        ranked = sorted(scores,key=lambda symbol: (-scores[symbol]['score'],symbol))
        selected = tuple(sorted(ranked[:p.positions])) if p.exposure else ()
        if selected == self.selected:
            return None
        self.selected = selected
        weights = Calculation.equal_weights(selected,len(selected),p.exposure) if selected else {}
        return TargetSet(self.strategy_id,frame.at,weights,{symbol:'margin_debt_and_book_yield' for symbol in selected})
