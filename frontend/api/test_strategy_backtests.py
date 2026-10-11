from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from frontend.api.test_workbench import HttpContractTests
from frontend.api.workbench.router import create_router

class EmptyResearch:
    def list(self): return []
    def detail(self,*args): raise KeyError()

class BacktestAPITests(unittest.IsolatedAsyncioTestCase):
    call = HttpContractTests.call
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        config={'storage':'workbench','sources':[],'research':{}}
        app=FastAPI(); app.include_router(create_router(self.root,config,providers={},research=EmptyResearch()))
        self.app=app
        self.request={'module':'plugins.strategies.breadth_low_amount_reversal','params':{},'initial_cash':100000,'start':'2020-01-01','end':'2020-12-31','dataset_id':''}
    def tearDown(self): self.temp.cleanup()
    async def test_catalog_uses_declared_parameter_schema(self):
        status,response=await self.call(self.app,'GET','/api/workbench/strategies')
        self.assertEqual(status,200)
        item=response['items'][0]
        self.assertEqual(item['defaults']['positions'],8)
        self.assertEqual(next(p for p in item['parameters'] if p['key']=='positions')['type'],'integer')
        self.assertFalse(response['availability']['ready'])
    async def test_preflight_has_effective_parameters_requirements_and_hash(self):
        status,a=await self.call(self.app,'POST','/api/workbench/backtests/preflight',self.request)
        self.assertEqual(status,200)
        status,b=await self.call(self.app,'POST','/api/workbench/backtests/preflight',{**self.request,'params':{'trend_window':160}})
        self.assertEqual(status,200)
        self.assertFalse(a['ready'])
        self.assertNotEqual(a['config_hash'],b['config_hash'])
        self.assertNotEqual(a['requirements']['history_bars'],b['requirements']['history_bars'])
    async def test_missing_history_cannot_publish_returns(self):
        status,response=await self.call(self.app,'POST','/api/workbench/backtests',self.request)
        self.assertEqual(status,409)
        self.assertEqual(response['detail']['code'],'backtest_blocked')
        self.assertTrue(response['detail']['issues'])
        self.assertFalse(any(self.root.glob('runs/*/metrics.json')))
    async def test_wrong_parameters_fail_before_history(self):
        for params in ({'positions':'8'},{'positions':True},{'wrong':3}):
            status,response=await self.call(self.app,'POST','/api/workbench/backtests/preflight',{**self.request,'params':params})
            self.assertEqual(status,422)
            self.assertEqual(response['detail']['code'],'invalid_parameters')

    async def test_successful_file_run_survives_catalog_and_detail_reload(self):
        from tests.history_fixture import make_snapshot
        from frontend.api.workbench.research import FileResearch
        request={**self.request,'dataset_id':'fixture','params':{'positions':4},'start':'2020-01-31','end':'2020-02-04'}
        config={'storage':'workbench','sources':[],'research':{'roots':['runs_parameters']},
                'backtests':make_snapshot(self.root)}
        app=FastAPI(); app.include_router(create_router(self.root,config,providers={},research=FileResearch(self.root,config['research'])))
        status,result=await self.call(app,'POST','/api/workbench/backtests',request)
        self.assertEqual(status,200,result)
        status,listed=await self.call(app,'GET','/api/workbench/runs')
        self.assertEqual(status,200)
        self.assertEqual(listed['items'][0]['id'],result['id'])
        status,detail=await self.call(app,'GET','/api/workbench/runs/'+result['id'])
        self.assertEqual(status,200)
        for key in ('parameters','equity','config_hash','data_hash','execution_hash','code_hash'):
            self.assertEqual(detail[key],result[key],key)
        self.assertEqual(detail['parameters']['positions'],4)

    async def test_removed_cached_plugin_is_rejected_before_data_checks(self):
        import importlib, sys
        from unittest.mock import patch
        from strategy import load_strategy
        package=importlib.import_module('plugins.strategies')
        importlib.import_module('plugins.strategies.moving_average_cross')
        name='plugins.strategies.removal_probe_api'
        source=self.root/'removal_probe_api.py'
        source.write_text('from plugins.strategies.moving_average_cross import NAME, Params, Strategy\n')
        try:
            with patch.object(package,'__path__',[str(self.root)]):
                load_strategy(name,{})
                source.unlink()
                for path in ('/api/workbench/backtests/preflight','/api/workbench/backtests'):
                    status,response=await self.call(self.app,'POST',path,{**self.request,'module':name})
                    self.assertEqual(status,422)
                    self.assertIn('策略文件已移除或不存在',response['detail']['message'])
                status,response=await self.call(self.app,'GET','/api/workbench/strategies')
                self.assertEqual(status,200)
                self.assertEqual(response['items'],[])
        finally:
            sys.modules.pop(name,None)
