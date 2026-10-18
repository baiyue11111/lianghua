"""Quality trend proxy using only the strategy-declared signal series."""
from dataclasses import dataclass, field
from datetime import date

from core.calculation import Calculation
from protocol import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params
from ._ranked_signal import histories, weights

NAME = '优质股趋势'


@dataclass(frozen=True)
class Params:
    positions: int = field(default=3, metadata={'label': '持股数量', 'minimum': 1, 'maximum': 100})
    fast_window: int = field(default=20, metadata={'label': '快线窗口', 'minimum': 2, 'maximum': 500, 'unit': '交易日'})
    slow_window: int = field(default=120, metadata={'label': '慢线窗口', 'minimum': 20, 'maximum': 2000, 'unit': '交易日'})
    rebalance: str = field(default='daily', metadata={'label': '调仓频率', 'options': ('daily', 'monthly')})
    exposure: float = field(default=1., metadata={'label': '总仓位', 'minimum': 0, 'maximum': 1})
    sort_desc: bool = field(default=True, metadata={'label': '降序'})

    def __post_init__(self):
        validate_params(self)
        if self.fast_window >= self.slow_window: raise ValueError('fast_window must be less than slow_window')


class Strategy:
    strategy_id = 'quality_trend'

    def __init__(self, params): self.params, self.selected = params, None

    def requirements(self): return DataRequest(('signal_close',), self.params.slow_window)

    def generate(self, frame: MarketFrame, account: AccountSnapshot):
        if type(frame.context.get('trading')) is not bool: raise ValueError('trading phase must be explicit')
        if self.params.rebalance == 'monthly':
            if 'next_session' not in frame.context: raise ValueError('next_session is required for monthly rebalance')
            following = date.fromisoformat(frame.context['next_session'])
            if following <= frame.at.date(): raise ValueError('next_session must follow decision session')
            if (frame.at.year, frame.at.month) == (following.year, following.month): return None
        if not frame.context['trading']: return None
        p = self.params
        candidates = []
        for symbol, prices in histories(frame, p.slow_window).items():
            fast = Calculation.sma(prices, p.fast_window)
            slow = Calculation.sma(prices, p.slow_window)
            if fast > slow: candidates.append((fast / slow, symbol))
        selected = Calculation.select(candidates, p.positions, lambda row: row[0], p.sort_desc)
        if selected == self.selected: return None
        for symbol, shares in account.positions.items():
            if shares <= 0: continue
            if frame.history[symbol][-1].fields['signal_close'] is None:
                return None
        self.selected = selected
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: 'fast_above_slow' for s in selected})
