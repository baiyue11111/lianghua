"""Weekly, long-only momentum/reversal switch using observed adjusted prices.

Market path = cumulative cross-sectional median daily log returns. Its efficiency
is absolute net change / total absolute changes. Separate entry/exit thresholds
reduce switching. A falling trend holds cash; a range buys short-term losers.
Only securities with a complete, aligned lookback participate. This synthetic
market path is not an investable index. No claim of predictive regime labels.
"""
from dataclasses import dataclass, field
from datetime import date
from math import fsum, isfinite, log
from statistics import median

from core.calculation import Calculation
from protocol import DataRequest, TargetSet
from strategy import validate_params

NAME = '趋势与震荡切换'


@dataclass(frozen=True)
class Params:
    window: int = field(default=60, metadata={'label':'状态窗口','minimum':2,'maximum':2000})
    momentum_window: int = field(default=60, metadata={'label':'动量窗口','minimum':1,'maximum':2000})
    reversal_window: int = field(default=5, metadata={'label':'反转窗口','minimum':1,'maximum':2000})
    enter: float = field(default=.30, metadata={'label':'进入趋势阈值','minimum':0,'maximum':1})
    exit: float = field(default=.15, metadata={'label':'退出趋势阈值','minimum':0,'maximum':1})
    positions: int = field(default=20, metadata={'label':'最多持股数','minimum':1,'maximum':1000})
    exposure: float = field(default=1., metadata={'label':'趋势仓位','minimum':0,'maximum':1})
    range_exposure: float = field(default=.5, metadata={'label':'震荡仓位','minimum':0,'maximum':1})

    def __post_init__(self):
        validate_params(self)
        if self.exit >= self.enter:
            raise ValueError('exit must be less than enter')


class Strategy:
    strategy_id = 'regime'

    def __init__(self, params: Params):
        self.params = params
        self.trending = False

    def requirements(self):
        p = self.params
        return DataRequest(('signal_close',),max(p.window,p.momentum_window,p.reversal_window)+1)

    def generate(self, frame, account):
        if type(frame.context['trading']) is not bool:
            raise ValueError('trading phase must be explicit')
        if not frame.context['trading']:
            return None
        following = date.fromisoformat(frame.context['next_session'])
        if following <= frame.at.date():
            raise ValueError('next_session must follow decision session')
        if frame.at.isocalendar()[:2] == following.isocalendar()[:2]:
            return None
        p = self.params
        size = self.requirements().history_bars
        times = sorted({row.at for rows in frame.history.values() for row in rows})
        if times and times[-1] != frame.at:
            raise ValueError('history must end at decision session')
        prices = {}
        if len(times) >= size:
            times = times[-size:]
            for symbol, rows in sorted(frame.history.items()):
                observed = {}
                for row in rows:
                    if row.at in observed:
                        raise ValueError('duplicate history timestamp')
                    value = row.fields['signal_close']
                    if value is not None and (type(value) not in (int,float) or not isfinite(value) or value <= 0):
                        raise ValueError('signal_close must be finite and positive')
                    observed[row.at] = value
                values = [observed.get(at) for at in times]
                if all(value is not None for value in values):
                    prices[symbol] = [log(value) for value in values]
        if not prices:
            self.trending = False
            return TargetSet(self.strategy_id,frame.at,{}, {})
        moves = [median(values[i]-values[i-1] for values in prices.values())
                 for i in range(size-p.window,size)]
        change = fsum(moves)
        distance = fsum(abs(move) for move in moves)
        efficiency = abs(change)/distance if distance else 0.
        if efficiency >= p.enter:
            self.trending = True
        elif efficiency <= p.exit:
            self.trending = False
        candidates = []
        if not self.trending or change > 0:
            window = p.momentum_window if self.trending else p.reversal_window
            for symbol, values in prices.items():
                score = values[-1]-values[-window-1]
                score = score if self.trending else -score
                if score > 0:
                    candidates.append((score,symbol))
        selected = Calculation.select(candidates,p.positions,lambda row: row[0])
        gross = p.exposure if self.trending else p.range_exposure
        weights = Calculation.equal_weights(selected,len(selected),gross) if selected and gross else {}
        reason = 'trend_momentum' if self.trending else 'range_reversal'
        return TargetSet(self.strategy_id,frame.at,weights,{symbol:reason for symbol in weights})
