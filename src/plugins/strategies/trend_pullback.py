"""Trend pullback strategy with a declared signal price and tunable windows."""
from dataclasses import dataclass, field
from datetime import date

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
    rebalance: str = field(default='daily', metadata={'label': '调仓频率', 'options': ['daily', 'monthly']})

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
        if p.rebalance == 'monthly':
            following = date.fromisoformat(frame.context['next_session'])
            if following <= frame.at.date():
                raise ValueError('next_session must follow decision session')
            if (frame.at.year, frame.at.month) == (following.year, following.month):
                return None
        held_rows = {}
        for symbol, shares in account.positions.items():
            if shares <= 0:
                continue
            rows = frame.history[symbol]
            for row in rows:
                if 'signal_close' not in row.fields:
                    raise KeyError('signal_close')
            held_rows[symbol] = rows

        prices_by_symbol = histories(frame, p.trend_window)
        if any(symbol not in prices_by_symbol for symbol in held_rows):
            return None

        score_index = {'drawdown': 0, 'recovery': 1}[p.sort_by]
        score = (lambda row: (row[0], row[1])) if p.sort_by == 'drawdown' else (lambda row: row[score_index])
        held_candidates = []
        for symbol in sorted(held_rows):
            prices = prices_by_symbol[symbol]
            trend = Calculation.sma(prices, p.trend_window)
            if prices[-1] > trend:
                recent = prices[-p.pullback_window:]
                drawdown = 1 - recent[-1] / max(prices[-p.trend_window:])
                recovery = recent[-1] / recent[0]
                held_candidates.append((drawdown, recovery, symbol))
        held = Calculation.select(held_candidates, p.positions, score, p.sort_desc,
                                  symbol=lambda row: row[2])
        held_symbols = set(held)
        candidates = []
        for symbol, prices in prices_by_symbol.items():
            if symbol in held_symbols:
                continue
            trend = Calculation.sma(prices, p.trend_window)
            recent = prices[-p.pullback_window:]
            peak = max(prices[-p.trend_window:])
            drawdown = 1 - recent[-1] / peak
            if recent[-1] > trend and drawdown >= p.drawdown:
                recovery = recent[-1] / recent[0]
                candidates.append((drawdown, recovery, symbol))
        added = Calculation.select(candidates, p.positions - len(held), score, p.sort_desc,
                                   symbol=lambda row: row[2])
        selected = tuple(held) + added
        if selected == self.selected:
            return None
        self.selected = selected
        return TargetSet(self.strategy_id, frame.at, weights(selected, p.exposure), {s: 'trend_then_pullback' for s in selected})
