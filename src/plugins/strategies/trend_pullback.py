"""Trend pullback strategy with a declared signal price and tunable windows."""
from dataclasses import dataclass, field

from core.calculation import Calculation
from protocal import AccountSnapshot, DataRequest, MarketFrame, TargetSet
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
    sort_by: str = field(default='drawdown', metadata={'label': '排序指标', 'options': ['drawdown', 'recovery']})
    sort_desc: bool = field(default=True, metadata={'label': '降序'})

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
        # Keep the portfolio until every held security has an observed signal.
        if any(frame.history[symbol][-1].fields['signal_close'] is None
               for symbol in account.positions):
            return None
        p = self.params
        candidates = []
        for symbol, prices in histories(frame, p.trend_window).items():
            trend = Calculation.sma(prices, p.trend_window)
            recent = prices[-p.pullback_window:]
            peak = max(prices[-p.trend_window:])
            drawdown = 1 - recent[-1] / peak
            if recent[-1] > trend and drawdown >= p.drawdown:
                recovery = recent[-1] / recent[0]
                candidates.append((drawdown, recovery, symbol))
        score_index = {'drawdown': 0, 'recovery': 1}[p.sort_by]
        score = (lambda row: (row[0], row[1])) if p.sort_by == 'drawdown' else (lambda row: row[score_index])
        selected = Calculation.select(candidates, p.positions, score, p.sort_desc)
        if selected == self.selected:
            return None
        self.selected = selected
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: 'trend_then_pullback' for s in selected})
