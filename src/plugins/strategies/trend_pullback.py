"""Trend pullback strategy with a declared signal price and tunable windows."""
from dataclasses import dataclass, field

from core.contracts import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params
from ._ranked_signal import histories, weights

NAME = '趋势回调'


@dataclass(frozen=True)
class Params:
    positions: int = field(default=3, metadata={'label': '持股数量', 'minimum': 1, 'maximum': 100})
    trend_window: int = field(default=120, metadata={'label': '趋势窗口', 'minimum': 20, 'maximum': 2000, 'unit': '交易日'})
    pullback_window: int = field(default=20, metadata={'label': '回调窗口', 'minimum': 2, 'maximum': 500, 'unit': '交易日'})
    drawdown: float = field(default=.08, metadata={'label': '回调幅度', 'minimum': 0, 'maximum': 1})
    exposure: float = field(default=1., metadata={'label': '总仓位', 'minimum': 0, 'maximum': 1})

    def __post_init__(self):
        validate_params(self)
        if self.pullback_window >= self.trend_window:
            raise ValueError('pullback_window must be less than trend_window')


class Strategy:
    strategy_id = 'trend_pullback'

    def __init__(self, params):
        self.params = params
        self.selected = None

    def requirements(self):
        return DataRequest(('signal_close',), self.params.trend_window)

    def generate(self, frame: MarketFrame, account: AccountSnapshot):
        if type(frame.context.get('trading')) is not bool:
            raise ValueError('trading phase must be explicit')
        if not frame.context['trading']:
            return None
        p = self.params
        candidates = []
        for symbol, prices in histories(frame, p.trend_window).items():
            trend = sum(prices[-p.trend_window:]) / p.trend_window
            recent = prices[-p.pullback_window:]
            peak = max(prices[-p.trend_window:])
            drawdown = 1 - recent[-1] / peak
            if recent[-1] > trend and drawdown >= p.drawdown:
                candidates.append((drawdown, recent[-1] / recent[0], symbol))
        selected = tuple(symbol for _, _, symbol in sorted(candidates, key=lambda x: (-x[0], -x[1], x[2]))[:p.positions])
        if selected == self.selected:
            return None
        self.selected = selected
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: 'trend_then_pullback' for s in selected})
