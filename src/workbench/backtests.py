"""Backtest catalog and preflight service; execution belongs to Kernel."""
from __future__ import annotations

from pathlib import Path

from application import load_adapter, prepare_backtest
from strategy import canonical_hash, catalog
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort


class Backtests:
    def __init__(self,root:Path,config:dict,data:DataPort):
        self.root=root; self.config=config; self.data=data
        self.adapter=load_adapter(data,config)

    def availability(self):
        datasets=self.adapter.datasets() if self.adapter is not None else []
        issues=[] if datasets else [{'code':'historical_adapter_missing','message':'尚无已登记的历史快照。需要原始行情、复权来源、交易日历、公司行动及账户税费输入。'}]
        return {'ready':bool(datasets),'datasets':datasets,'issues':issues}

    def catalog(self):
        return {'items':catalog(),'availability':self.availability()}

    def _prepare(self,request):
        return prepare_backtest(
            request,
            self.root,
            adapter=self.adapter,
            missing_adapter_issues=self.availability()['issues'],
            data=self.data,
        )

    def preflight(self,request):
        return self._result(self._prepare(request))

    @staticmethod
    def _result(plan):
        return {'ready':not plan.issues,'effective_params':plan.values['params'],
                'requirements':plan.values['requirements'],
                'config_hash':canonical_hash(plan.values),'effective_config':plan.values,
                'issues':list(plan.issues)}
