"""双均线：短均线高于长均线时持有，选中集合变化时等权调仓。"""
from dataclasses import dataclass, field
from math import isfinite

from core.contracts import DataRequest, TargetSet
from strategy import validate_params
from ..allocators.equal_weight import equal_weights

NAME='双均线'


@dataclass(frozen=True)
class Params:
    fast_window:int=field(default=20,metadata={'label':'短均线窗口','minimum':1,'maximum':2000,'unit':'交易日'})
    slow_window:int=field(default=60,metadata={'label':'长均线窗口','minimum':2,'maximum':2000,'unit':'交易日'})
    exposure:float=field(default=1.,metadata={'label':'总仓位','minimum':0,'maximum':1})

    def __post_init__(self):
        validate_params(self)
        if self.fast_window>=self.slow_window:
            raise ValueError('fast_window must be less than slow_window')


class Strategy:
    strategy_id='moving_average_cross'

    def __init__(self,params:Params):
        self.params=params
        self.selected=None

    def requirements(self):
        return DataRequest(('signal_close',),self.params.slow_window)

    def generate(self,frame,account):
        if type(frame.context['trading']) is not bool:
            raise ValueError('trading phase must be explicit')
        if not frame.context['trading']:
            return None
        if not frame.history:
            raise ValueError('strategy requires observed history')
        selected=[]
        p=self.params
        for symbol,rows in sorted(frame.history.items()):
            prices=[]
            for row in rows:
                value=row.fields['signal_close']
                if value is None:
                    continue
                if type(value) not in (int,float) or not isfinite(value) or value<=0:
                    raise ValueError('signal_close must be finite and positive')
                prices.append(value)
            if len(prices)<p.slow_window:
                return None
            if sum(prices[-p.fast_window:])/p.fast_window>sum(prices[-p.slow_window:])/p.slow_window and p.exposure>0:
                selected.append(symbol)
        selected=tuple(selected)
        if selected==self.selected:
            return None
        self.selected=selected
        weights=equal_weights(selected,len(selected),p.exposure) if selected else {}
        return TargetSet(self.strategy_id,frame.at,weights,{symbol:'fast_mean_above_slow_mean' for symbol in selected})
