import unittest,tempfile
from pathlib import Path
from market import build_data
from backend import Benchmarks, KernelBacktestPort, Research

class BenchmarkTests(unittest.TestCase):
    def test_compare_benchmark_does_not_require_trade_bar_fields(self):
        """An index comparison series may publish close without stock turnover fields."""
        import hashlib
        import json
        import pyarrow.parquet as pq
        from tests.history_fixture import make_snapshot, update_manifest

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path).select(["code", "date", "close"])
            pq.write_table(table, path)
            manifest = json.loads((root / "data/source/fixture/manifest.json").read_text())
            for item in manifest["files"]:
                if item["file"] == "daily/bars.parquet":
                    item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            update_manifest(root, files=manifest["files"])

            data = build_data(root)
            service = Benchmarks(data, [{
                "id": "hs300", "name": "沪深300", "source_id": "index",
                "dataset_id": "fixture", "code": "sh.600003", "provider": "test",
                "currency": "CNY", "return_type": "price",
            }])
            result = service.series("hs300", "2020-01-31", "2020-02-03")
            self.assertEqual(result["points"], [
                {"at": "2020-01-31", "close": 11.0},
                {"at": "2020-02-03", "close": 11.0},
            ])

    def test_daily_benchmark_reads_the_canonical_query_not_collection_snapshot(self):
        import pyarrow.parquet as pq
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as d:
            root = Path(d); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path); rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] == "2020-01-31":
                    row["close"] = row["open"] = row["high"] = row["low"] = 200.0
                    row["amount"] = row["volume"] * 200.0
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            data = build_data(root)
            import hashlib, json
            from tests.history_fixture import update_manifest
            manifest = data.manifest('fixture')
            for ref in manifest['files']:
                if ref['file'] == 'daily/bars.parquet':
                    ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            update_manifest(root, files=manifest['files'])
            data.write('sources', 'legacy', 'records.json', b'{"records":[{"date":"2020-01-31","code":"sh.600003","close":100}]}')
            service = Benchmarks(data, [{
                "id": "canonical", "name": "canonical", "source_id": "legacy",
                "dataset_id": "fixture", "code": "sh.600003", "provider": "test",
                "currency": "CNY", "return_type": "price",
            }])
            result = service.series("canonical", "2020-01-31", "2020-01-31")
            self.assertEqual(result["points"], [{"at": "2020-01-31", "close": 200.0}])

    def test_configured_index_reads_evidence_without_supplier_coupling(self):
        with tempfile.TemporaryDirectory() as d:
            from tests.history_fixture import make_snapshot
            make_snapshot(Path(d)); data=build_data(Path(d))
            service=Benchmarks(data,[{'id':'hs300','name':'基准','source_id':'unused','dataset_id':'fixture','code':'sh.600003','provider':'registered','currency':'CNY','return_type':'price'}])
            result=service.series('hs300','2020-01-31','2020-02-03')
            self.assertEqual(result['points'],[{'at':'2020-01-31','close':11.0},{'at':'2020-02-03','close':11.0}])
            self.assertTrue(result['snapshot_id'])
            with self.assertRaises(KeyError):service.series('unknown','2020-01-31','2020-02-03')
    def test_no_snapshot_and_bad_prices_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            data=build_data(Path(d)); spec={'id':'i','name':'指数','source_id':'s','code':'x','provider':'test','currency':'CNY','return_type':'price'}
            service=Benchmarks(data,[spec])
            with self.assertRaisesRegex(ValueError,'dataset_id'):service.series('i','2026-09-29','2026-09-30')
            from tests.history_fixture import make_snapshot
            from protocol import DataError
            make_snapshot(Path(d))
            service=Benchmarks(data,[{**spec, 'dataset_id':'fixture', 'code':'sh.600003'}])
            import json
            data.write('sources','fixture','daily/bars.parquet',b'corrupt')
            with self.assertRaises(DataError):service.series('i','2020-01-31','2020-02-03')

    def test_requested_account_dates_must_be_fully_covered(self):
        with tempfile.TemporaryDirectory() as d:
            data=build_data(Path(d))
            from tests.history_fixture import make_snapshot
            make_snapshot(Path(d))
            spec={'id':'i','name':'基准','source_id':'s','dataset_id':'fixture','code':'sh.600003','provider':'test','currency':'CNY','return_type':'price'}
            service=Benchmarks(data,[spec])
            with self.assertRaisesRegex(ValueError,'日期不全'):
                service.series('i','2020-01-31','2020-02-03',dates=('2020-01-31','2020-02-01','2020-02-03'))

class BenchmarkPortTests(unittest.TestCase):
    def test_preflight_and_run_keep_config_hash_when_benchmark_is_added(self):
        from backend import _result_data
        from tests.test_core_backtest import BacktestTests, request

        kernel = BacktestTests().make_kernel()
        request_value = request(run_id="benchmark", benchmark_id="hs300")
        prepared = kernel.prepare(request_value)
        result = kernel.run(request_value)

        class Benchmark:
            def series(self, identity, start, end, *, dates):
                return {
                    "id": identity, "snapshot_id": "snapshot-hash",
                    "return_type": "price",
                    "points": [{"at": dates[0], "close": 100.0}],
                }

        value = _result_data(result, request_value, Benchmark())
        self.assertEqual(value["config_hash"], prepared.config_hash)
        self.assertNotIn("benchmark", value["effective_config"])
        self.assertEqual(value["benchmark"]["snapshot_id"], "snapshot-hash")
        self.assertEqual(value["provenance"]["benchmark"]["data_hash"], "snapshot-hash")

    def test_benchmark_protocol_port_and_missing_data(self):
        with tempfile.TemporaryDirectory() as d:
            spec={'id':'hs300','name':'沪深300','source_id':'index','code':'sh.000300','provider':'BaoStock','currency':'CNY','return_type':'price'}
            data=build_data(Path(d))
            port=KernelBacktestPort(data, lambda *args, **kwargs: object(),
                                    Benchmarks(data, [spec]), datasets=(),
                                    research=Research(data))
            self.assertEqual(port.handle('research.benchmarks',{}),[spec])
            request={'id':'hs300','start':'2026-09-29','end':'2026-09-30'}
            with self.assertRaises(ValueError):port.handle('research.benchmark',request)
            from tests.history_fixture import make_snapshot
            make_snapshot(Path(d))
            spec.update(dataset_id='fixture', code='sh.600003')
            port=KernelBacktestPort(data, lambda *args, **kwargs: object(), Benchmarks(data,[spec]))
            series=port.handle('research.benchmark',{**request,'start':'2020-01-31','end':'2020-02-03'})
            self.assertEqual(series['points'][0]['close'],11.0)
            with self.assertRaises(ValueError):port.handle('research.benchmark',{**request,'start':'not-a-date'})

class ResearchEndpointTests(unittest.TestCase):
    def test_plot_sampling_keeps_final_observation(self):
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
