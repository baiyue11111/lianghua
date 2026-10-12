import unittest,tempfile
from pathlib import Path
from protocal import build_data
from collect.data import Store
from workbench.benchmark import Benchmarks

class BenchmarkTests(unittest.TestCase):
    def test_configured_index_reads_evidence_without_supplier_coupling(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(build_data(Path(d))); snapshot=store.snapshot('index',{'records':[{'date':'2026-09-29','code':'sh.000300','close':'4000'},{'date':'2026-09-30','code':'sh.000300','close':'4040'}],'raw':[{'response':'original'}],'scope':{},'data_as_of':'2026-09-30'},{})
            service=Benchmarks(store,[{'id':'hs300','name':'沪深300','source_id':'index','code':'sh.000300','provider':'BaoStock','currency':'CNY','return_type':'price'}])
            result=service.series('hs300','2026-09-29','2026-09-30')
            self.assertEqual(result['points'],[{'at':'2026-09-29','close':4000.0},{'at':'2026-09-30','close':4040.0}])
            self.assertEqual(result['snapshot_id'],snapshot['snapshot_id'])
            with self.assertRaises(KeyError):service.series('unknown','2026-09-29','2026-09-30')
    def test_no_snapshot_and_bad_prices_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(build_data(Path(d))); spec={'id':'i','name':'指数','source_id':'s','code':'x','provider':'test','currency':'CNY','return_type':'price'}
            service=Benchmarks(store,[spec])
            with self.assertRaisesRegex(ValueError,'未同步'):service.series('i','2026-09-29','2026-09-30')
            store.snapshot('s',{'records':[{'date':'2026-09-29','code':'x','close':'0'}],'raw':[],'scope':{},'data_as_of':'2026-09-29'},{})
            with self.assertRaisesRegex(ValueError,'收盘价'):service.series('i','2026-09-29','2026-09-30')

    def test_requested_account_dates_must_be_fully_covered(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(build_data(Path(d)))
            spec={'id':'i','name':'指数','source_id':'s','code':'x','provider':'test','currency':'CNY','return_type':'price'}
            store.snapshot('s',{'records':[
                {'date':'2026-09-29','code':'x','close':'100'},
                {'date':'2026-10-01','code':'x','close':'110'},
            ],'raw':[],'scope':{},'data_as_of':'2026-10-01'}, {})
            service=Benchmarks(store,[spec])
            with self.assertRaisesRegex(ValueError,'日期不全'):
                service.series('i','2026-09-29','2026-10-01',dates=('2026-09-29','2026-09-30','2026-10-01'))

class BenchmarkPortTests(unittest.TestCase):
    def test_benchmark_protocol_port_and_missing_data(self):
        from workbench.port import WorkbenchPort
        class Research:
            def list(self):return []
        with tempfile.TemporaryDirectory() as d:
            spec={'id':'hs300','name':'沪深300','source_id':'index','code':'sh.000300','provider':'BaoStock','currency':'CNY','return_type':'price'}
            config={'storage':'data','sources':[{'id':'index','name':'指数','frequency':'日','defaults':{}}],'research':{},'benchmarks':[spec]}
            data=build_data(Path(d))
            port=WorkbenchPort(Path(d),config,data,store=Store(data),research=Research())
            self.assertEqual(port.handle('workbench.benchmarks',{}),[spec])
            request={'id':'hs300','start':'2026-09-29','end':'2026-09-30'}
            with self.assertRaises(ValueError):port.handle('workbench.benchmark',request)
            port.store.snapshot('index',{'records':[{'date':'2026-09-29','code':'sh.000300','close':'4000'}],'raw':[{}],'scope':{},'data_as_of':'2026-09-29'},{})
            series=port.handle('workbench.benchmark',request);self.assertEqual(series['points'][0]['close'],4000)
            with self.assertRaises(ValueError):port.handle('workbench.benchmark',{**request,'start':'not-a-date'})

class ResearchEndpointTests(unittest.TestCase):
    def test_plot_sampling_keeps_final_observation(self):
        from workbench.research import Research
        import json,csv
        from datetime import date,timedelta
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();data=build_data(root)
            data.write('runs','r','manifest.json',json.dumps({'run_id':'r','strategy_id':'test','status':'unverified'}).encode())
            data.write('runs','r','metrics.json',b'{}')
            equity=[{'at':(date(2020,1,1)+timedelta(days=i)).isoformat(),'equity':100+i} for i in range(802)]
            data.write('runs','r','result.json',json.dumps({'trades':[],'equity':equity}).encode())
            data.write('runs','r','trace.json',b'{}')
            research=Research(data);identity=research.list()[0]['id']
            result=research.detail(identity)
            self.assertEqual(result['equity'][-1]['equity'],901)
            self.assertEqual(len({p['at'] for p in result['equity']}),len(result['equity']))
