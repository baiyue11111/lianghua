"""HTTP adapter for the shared backtest application service."""
from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4

from application import execute_backtest, load_adapter, prepare_backtest
from strategy import canonical_hash, catalog, code_hash


class BacktestBlocked(ValueError):
    def __init__(self, preflight):
        self.preflight=preflight
        super().__init__('历史回测输入未通过检查')


class Backtests:
    def __init__(self,root:Path,config:dict,store):
        self.root=root; self.config=config; self.store=store
        self.adapter=load_adapter(root,config)

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
        )

    def preflight(self,request):
        return self._result(self._prepare(request))

    @staticmethod
    def _result(plan):
        return {'ready':not plan.issues,'effective_params':plan.values['params'],
                'requirements':plan.values['requirements'],
                'config_hash':canonical_hash(plan.values),'effective_config':plan.values,
                'issues':list(plan.issues)}

    def run(self,request):
        plan=self._prepare(request)
        preflight=self._result(plan)
        run_id=uuid4().hex
        started=datetime.now(UTC).isoformat()
        record={'run_id':run_id,'started_at':started,'request':request,'preflight':preflight}
        if not preflight['ready']:
            self.store.write('backtest_requests',run_id,{**record,'status':'blocked'})
            raise BacktestBlocked(preflight)
        try:
            result=execute_backtest(plan,run_id)
        except Exception as exc:
            self.store.write('backtest_requests',run_id,{**record,'status':'failed','error':f'{type(exc).__name__}: {exc}'})
            raise
        result.update(parameters=preflight['effective_params'],effective_config=preflight['effective_config'],
                      config_hash=preflight['config_hash'],code_hash=code_hash())
        self.store.write('backtest_requests',run_id,{**record,'status':'succeeded','result_id':result['id']})
        return result
