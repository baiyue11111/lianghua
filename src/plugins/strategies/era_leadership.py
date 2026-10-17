"""Momentum leadership strategy with tunable lookback and portfolio size."""
from dataclasses import dataclass, field
from datetime import date

from core.calculation import Calculation
from protocal import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params
from ._ranked_signal import histories, weights

NAME = '强势行业龙头'


@dataclass(frozen=True)
class Params:
    positions: int = field(default=3, metadata={'label': '持股数量', 'minimum': 1, 'maximum': 100})
    momentum_window: int = field(default=120, metadata={'label': '动量窗口', 'minimum': 20, 'maximum': 2000, 'unit': '交易日'})
    rebalance: str = field(default='daily', metadata={'label': '调仓频率', 'options': ('daily', 'monthly')})
    exposure: float = field(default=1., metadata={'label': '总仓位', 'minimum': 0, 'maximum': 1})
    sort_desc: bool = field(default=True, metadata={'label': '降序'})

    def __post_init__(self): validate_params(self)


class Strategy:
    strategy_id = 'era_leadership'

    def __init__(self, params): self.params, self.selected = params, None

    def requirements(self): return DataRequest(('signal_close',), self.params.momentum_window)

    def generate(self, frame: MarketFrame, account: AccountSnapshot):
        if type(frame.context.get('trading')) is not bool: raise ValueError('trading phase must be explicit')
        if self.params.rebalance == 'monthly':
            if 'next_session' not in frame.context: raise ValueError('next_session is required for monthly rebalance')
            following = date.fromisoformat(frame.context['next_session'])
            if following <= frame.at.date(): raise ValueError('next_session must follow decision session')
            if (frame.at.year, frame.at.month) == (following.year, following.month): return None
        if not frame.context['trading']: return None
        # Keep the portfolio until every held security has an observed signal.
        if any(frame.history[symbol][-1].fields['signal_close'] is None
               for symbol in account.positions):
            return None
        p = self.params
        candidates = []
        for symbol, prices in histories(frame, p.momentum_window).items():
            base, last = prices[-p.momentum_window], prices[-1]
            if last > base: candidates.append((last / base, symbol))
        selected = Calculation.select(candidates, p.positions, lambda row: row[0], p.sort_desc)
        if selected == self.selected: return None
        self.selected = selected
        reason = 'highest_momentum' if p.sort_desc else 'lowest_momentum'
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: reason for s in selected})
