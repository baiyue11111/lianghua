"""市场宽度门控、低成交额反转；默认参数保留原规则。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import ceil
import numpy as np

from core.calculation import Calculation
from protocal import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params

NAME = '市场宽度 · 低成交额反转'


@dataclass(frozen=True)
class Params:
    positions: int = field(default=8,metadata={'label':'持股上限','minimum':1,'maximum':1000,'unit':'只'})
    amount_quantile: float = field(default=.10,metadata={'label':'低成交额比例','exclusive_minimum':0,'maximum':1})
    amount_floor: float = field(default=50_000_000.,metadata={'label':'日均成交额下限','minimum':0,'unit':'元'})
    entry_breadth: float = field(default=.60,metadata={'label':'入场宽度','minimum':0,'maximum':1})
    exit_breadth: float = field(default=.45,metadata={'label':'离场宽度','minimum':0,'maximum':1})
    trend_window: int = field(default=120,metadata={'label':'趋势均线窗口','minimum':1,'maximum':2000,'unit':'交易日'})
    return_window: int = field(default=20,metadata={'label':'反转窗口','minimum':1,'maximum':2000,'unit':'交易日'})
    amount_window: int = field(default=20,metadata={'label':'成交额窗口','minimum':1,'maximum':2000,'unit':'交易日'})
    breadth_window: int = field(default=5,metadata={'label':'宽度平滑窗口','minimum':1,'maximum':200,'unit':'交易日'})
    entry_cadence: str = field(default='month_end',metadata={'label':'首次入场','options':['month_end','on_qualification']})
    sort_by: str = field(default='return',metadata={'label':'候选排序','options':['return','amount']})
    sort_desc: bool = field(default=False,metadata={'label':'降序'})

    def __post_init__(self):
        validate_params(self)
        if not 0<=self.exit_breadth<self.entry_breadth<=1:
            raise ValueError('breadth thresholds must satisfy 0 <= exit < entry <= 1')


class Strategy:
    strategy_id='breadth_low_amount_reversal'

    def __init__(self,params:Params):
        self.params=params
        self.market_on=False

    def requirements(self)->DataRequest:
        p=self.params
        return DataRequest(('open','high','low','close','volume','amount','signal_close'),
                           max(p.trend_window,p.return_window+1,p.amount_window)+p.breadth_window-1,universe="CN_A_SHARE")

    def features(self,frame:MarketFrame):
        """Use only each security's own observed trading days, through this close."""
        if not frame.history: raise ValueError('strategy requires observed adjusted history')
        symbols=tuple(sorted(frame.history))
        times=sorted({row.at for rows in frame.history.values() for row in rows})
        if not times or times[-1]!=frame.at: raise ValueError('history must include decision session')
        index={at:i for i,at in enumerate(times)}
        arrays={name:np.full((len(symbols),len(times)),np.nan) for name in self.requirements().fields}
        for i,symbol in enumerate(symbols):
            for row in frame.history[symbol]:
                for name in arrays:
                    if name not in row.fields: raise ValueError(f'missing historical field {symbol}.{name}')
                    value=row.fields[name]
                    if value is not None and type(value) not in (int,float): raise ValueError(f'invalid historical field {symbol}.{name}')
                    arrays[name][i,index[row.at]]=np.nan if value is None else value
        mask=Calculation.valid_mask(*arrays.values())
        p=self.params
        amounts=Calculation.own_day_rolling_mean(arrays['amount'],p.amount_window,mask)
        trend=Calculation.own_day_rolling_mean(arrays['signal_close'],p.trend_window,mask)
        returns=Calculation.own_day_lagged_return(arrays['signal_close'],p.return_window,mask)
        eligible=np.isfinite(amounts)&(amounts>=p.amount_floor)
        denominator=eligible.sum(axis=0)
        numerator=(eligible&np.isfinite(trend)&(arrays['signal_close']>trend)).sum(axis=0)
        raw=np.divide(numerator,denominator,out=np.full(len(times),np.nan),where=denominator>0)
        recent=raw[-p.breadth_window:]
        breadth=float(np.mean(recent)) if len(recent)==p.breadth_window and np.isfinite(recent).all() else None
        candidates=[]
        for i,symbol in enumerate(symbols):
            if mask[i,-1] and eligible[i,-1] and arrays['signal_close'][i,-1]>trend[i,-1] and np.isfinite(returns[i,-1]):
                candidates.append((symbol,float(amounts[i,-1]),float(returns[i,-1])))
        return breadth,candidates

    def generate(self,frame:MarketFrame,account:AccountSnapshot)->TargetSet|None:
        p=self.params
        if 'next_session' not in frame.context: raise ValueError('next_session is required to determine month end')
        following=date.fromisoformat(frame.context['next_session'])
        if following<=frame.at.date(): raise ValueError('next_session must follow decision session')
        breadth,candidates=self.features(frame)
        if breadth is None:
            # No price or breadth substitution: remain in the previous gate state.
            # This is the declared original gate rule during unobserved breadth.
            pass
        elif self.market_on:
            self.market_on=breadth>=p.exit_breadth
        else:
            self.market_on=breadth>=p.entry_breadth
        if not self.market_on:
            return TargetSet(self.strategy_id,frame.at,{}, {'__market__':'market_breadth_exit'})
        held=bool(account.positions)
        if not held and (breadth is None or breadth<p.entry_breadth):
            return TargetSet(self.strategy_id,frame.at,{}, {'__market__':'wait_for_entry_breadth'})
        month_end=(frame.at.year,frame.at.month)!=(following.year,following.month)
        entry_due=not held and p.entry_cadence=='on_qualification'
        if not month_end and not entry_due: return None
        count=ceil(len(candidates)*p.amount_quantile)
        cold=sorted(candidates,key=lambda row:(row[1],row[0]))[:count]
        score_index = {'amount': 1, 'return': 2}[p.sort_by]
        symbols = Calculation.select(cold, p.positions, lambda row: row[score_index], p.sort_desc,
                                     symbol=lambda row: row[0])
        reason = 'low_amount_then_lowest_return' if p.sort_by == 'return' and not p.sort_desc else f'low_amount_then_{p.sort_by}'
        return TargetSet(self.strategy_id,frame.at,
                         Calculation.equal_weights(symbols,p.positions) if symbols else {},
                         {symbol:reason for symbol in symbols})
