"""Momentum leadership strategy with tunable lookback and portfolio size."""
from dataclasses import dataclass, field

from protocal import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params
from ._ranked_signal import histories, weights

NAME = '强势行业龙头'


@dataclass(frozen=True)
class Params:
    positions: int = field(default=3, metadata={'label': '持股数量', 'minimum': 1, 'maximum': 100})
    momentum_window: int = field(default=120, metadata={'label': '动量窗口', 'minimum': 20, 'maximum': 2000, 'unit': '交易日'})
    exposure: float = field(default=1., metadata={'label': '总仓位', 'minimum': 0, 'maximum': 1})

    def __post_init__(self): validate_params(self)


class Strategy:
    strategy_id = 'era_leadership'

    def __init__(self, params): self.params, self.selected = params, None

    def requirements(self): return DataRequest(('signal_close',), self.params.momentum_window)

    def generate(self, frame: MarketFrame, account: AccountSnapshot):
        if type(frame.context.get('trading')) is not bool: raise ValueError('trading phase must be explicit')
        if not frame.context['trading']: return None
        p = self.params
        candidates = []
        for symbol, prices in histories(frame, p.momentum_window).items():
            base, last = prices[-p.momentum_window], prices[-1]
            if last > base: candidates.append((last / base, symbol))
        selected = tuple(symbol for _, symbol in sorted(candidates, key=lambda x: (-x[0], x[1]))[:p.positions])
        if selected == self.selected: return None
        self.selected = selected
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: 'highest_momentum' for s in selected})
