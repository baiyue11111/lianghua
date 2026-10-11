import unittest,tempfile
from pathlib import Path
from frontend.api.workbench.store import Store
from frontend.api.workbench.benchmark import Benchmarks

class BenchmarkTests(unittest.TestCase):
    def test_configured_index_reads_evidence_without_supplier_coupling(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)); snapshot=store.snapshot('index',{'records':[{'date':'2026-09-29','code':'sh.000300','close':'4000'},{'date':'2026-09-30','code':'sh.000300','close':'4040'}],'raw':[{'response':'original'}],'scope':{},'data_as_of':'2026-09-30'},{})
            service=Benchmarks(store,[{'id':'hs300','name':'沪深300','source_id':'index','code':'sh.000300','provider':'BaoStock','currency':'CNY','return_type':'price'}])
            result=service.series('hs300','2026-09-29','2026-09-30')
            self.assertEqual(result['points'],[{'at':'2026-09-29','close':4000.0},{'at':'2026-09-30','close':4040.0}])
            self.assertEqual(result['snapshot_id'],snapshot['snapshot_id'])
            with self.assertRaises(KeyError):service.series('unknown','2026-09-29','2026-09-30')
    def test_no_snapshot_and_bad_prices_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)); spec={'id':'i','name':'指数','source_id':'s','code':'x','provider':'test','currency':'CNY','return_type':'price'}
            service=Benchmarks(store,[spec])
            with self.assertRaisesRegex(ValueError,'未同步'):service.series('i','2026-09-29','2026-09-30')
            store.snapshot('s',{'records':[{'date':'2026-09-29','code':'x','close':'0'}],'raw':[],'scope':{},'data_as_of':'2026-09-29'},{})
            with self.assertRaisesRegex(ValueError,'收盘价'):service.series('i','2026-09-29','2026-09-30')

from frontend.api import test_workbench as http_tests
class BenchmarkHttpTests(unittest.IsolatedAsyncioTestCase):
    call=http_tests.HttpContractTests.call
    async def test_benchmark_http_contract_and_missing_data(self):
        from fastapi import FastAPI
        from frontend.api.workbench.router import create_router
        class Research:
            def list(self):return []
        with tempfile.TemporaryDirectory() as d:
            spec={'id':'hs300','name':'沪深300','source_id':'index','code':'sh.000300','provider':'BaoStock','currency':'CNY','return_type':'price'}
            config={'storage':'data','sources':[{'id':'index','name':'指数','frequency':'日','defaults':{}}],'research':{},'benchmarks':[spec]}
            router=create_router(Path(d),config,{},Research());app=FastAPI();app.include_router(router)
            status,catalog=await self.call(app,'GET','/api/workbench/benchmarks');self.assertEqual(status,200);self.assertEqual(catalog,[spec])
            path='/api/workbench/benchmarks/hs300';query=b'start=2026-09-29&end=2026-09-30'
            status,_=await self.call(app,'GET',path,query=query);self.assertEqual(status,409)
            router.store.snapshot('index',{'records':[{'date':'2026-09-29','code':'sh.000300','close':'4000'}],'raw':[{}],'scope':{},'data_as_of':'2026-09-29'},{})
            status,series=await self.call(app,'GET',path,query=query);self.assertEqual(status,200);self.assertEqual(series['points'][0]['close'],4000)
            status,_=await self.call(app,'GET',path,query=b'start=not-a-date&end=2026-09-30');self.assertEqual(status,422)

class ResearchEndpointTests(unittest.TestCase):
    def test_plot_sampling_keeps_final_observation(self):
        from frontend.api.workbench.research import FileResearch
        import json,csv
        from datetime import date,timedelta
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();folder=root/'runs'/'r';(folder/'audit').mkdir(parents=True)
            (folder/'manifest.json').write_text(json.dumps({'run_id':'r','strategy_id':'test','status':'unverified'}))
            (folder/'metrics.json').write_text('{}')
            with (folder/'audit/equity.csv').open('w') as f:
                writer=csv.writer(f);writer.writerow(['trade_date','equity'])
                for i in range(802):writer.writerow([(date(2020,1,1)+timedelta(days=i)).isoformat(),100+i])
            research=FileResearch(root,{'roots':['runs']});identity=research.list()[0]['id']
            result=research.detail(identity)
            self.assertEqual(result['equity'][-1]['equity'],901)
            self.assertEqual(len({p['at'] for p in result['equity']}),len(result['equity']))
