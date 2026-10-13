from pathlib import Path
import tempfile
import unittest

from protocal import DataBus


def data_bus_check(root, adapter, config, request, requirements):
    from protocal import DataBus
    from storage import DataStore
    bus = DataBus(DataStore(root))
    bus.configure_reader(adapter)
    if request.get('dataset_id') not in adapter.entries:
        return [{'code': 'historical_dataset_missing', 'message': 'dataset is not registered'}]
    try:
        dataset = bus.query(adapter.build_query(request, requirements))
    except KeyError as error:
        code = 'historical_dataset_missing' if error.args[0] == 'historical_dataset_missing' else 'historical_input_missing'
        return [{'code': code, 'message': str(error)}]
    except Exception as error:
        return [{'code': 'historical_input_invalid', 'message': str(error)}]
    return adapter.check(dataset)


def kernel_result(root, config, request, strategy, loader=None):
    from datetime import datetime, timezone, time
    from decimal import Decimal
    from protocal import BacktestRequest
    from core.kernel import Kernel
    from history_data import HistoricalLake
    from plugins.data import Settlement
    from protocal import DataBus, _query, _result_data
    from storage import DataStore
    bus=DataBus(DataStore(root)); bus.configure_reader(HistoricalLake(bus, config))
    loader=loader or (lambda _name, _params: strategy)
    start=datetime.fromisoformat(request['start']).replace(tzinfo=timezone.utc)
    end=datetime.combine(datetime.fromisoformat(request['end']).date(),time.max,tzinfo=timezone.utc)
    value=BacktestRequest(request.get('run_id','test'),strategy.strategy_id,request.get('params',{}),
        request['dataset_id'],start,end,Decimal(str(request['initial_cash'])))
    return _result_data(Kernel(bus,loader,_query,Settlement).run(value))

class HistoricalDataTests(unittest.TestCase):
    def test_preflight_effective_fields_match_resolved_dataset_fields(self):
        from application import prepare_backtest
        from history_data import HistoricalLake
        from protocal import DataBus, parse_backtest_request
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            adapter = HistoricalLake(bus, config)
            bus.configure_reader(adapter)
            request = parse_backtest_request({
                'module': 'plugins.strategies.moving_average_cross',
                'params': {'fast_window': 2, 'slow_window': 3},
                'initial_cash': '100000', 'start': '2020-01-31',
                'end': '2020-02-04', 'dataset_id': 'fixture',
                'symbols': ['600000'], 'fields': ['signal_close'],
                'history': 3,
            })
            plan = prepare_backtest(request, root, adapter=adapter, data=bus)

            self.assertFalse(plan.issues)
            self.assertEqual(plan.values['fields'], plan.values['requirements']['fields'])

    def test_real_snapshot_without_universe_source_file_is_readable(self):
        from datetime import date
        from history_data import HistoricalLake
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            bus.configure_reader(HistoricalLake(bus, config))
            dataset = bus.query(DataQuery(
                'fixture', date(2020, 1, 31), date(2020, 2, 4),
                ('open', 'high', 'low', 'close', 'volume', 'amount', 'signal_close'),
                20, 'adjusted', symbols=('600003',), universe='dataset',
            ))

            self.assertEqual(dataset.scope['symbols'], ('600003',))
            self.assertNotIn('universe_source', dataset.scope)

    def test_verified_boolean_does_not_authorize_a_dataset(self):
        import json
        from history_data import HistoricalLake
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); snapshot=root/'data/sources/sample'; snapshot.mkdir(parents=True)
            (snapshot/'manifest.json').write_text(json.dumps({'verified':True,'schema':1}))
            from protocal import DataBus
            from storage import DataStore
            bus = DataBus(DataStore(root))
            adapter=HistoricalLake(bus,{'datasets':[{'id':'sample','name':'sample'}]})
            request={'dataset_id':'sample','module':'plugins.strategies.breadth_low_amount_reversal','params':{},'initial_cash':100000,'start':'2020-01-01','end':'2020-01-31'}
            from strategy import load_strategy
            issues=data_bus_check(Path(temp), adapter, adapter.config, request,
                                  load_strategy(request['module'],{}).requirements())
            self.assertTrue(issues)
            self.assertFalse(list(root.glob('runs/*')))

    def test_unknown_dataset_is_explicit_failure(self):
        from history_data import HistoricalLake
        from protocal import DataRequest
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            bus = DataBus(DataStore(Path(temp)))
            adapter=HistoricalLake(bus,{'datasets':[]})
            issues=data_bus_check(Path(temp), adapter, adapter.config,
                                  {'dataset_id':'missing'}, DataRequest(('close',),1))
            self.assertEqual(issues[0]['code'],'historical_dataset_missing')

    def test_real_files_execute_parameter_ab_with_one_strategy_file(self):
        from dataclasses import asdict
        from history_data import HistoricalLake
        from strategy import load_strategy
        from tests.history_fixture import make_snapshot
        from protocal import DataBus
        module='plugins.strategies.breadth_low_amount_reversal'
        a={'module':module,'params':{'positions':4},'initial_cash':100000,'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
        b={**a,'params':{'positions':8}}
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); snapshot=make_snapshot(root); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,snapshot)
            results=[]
            for index,request in enumerate((a,b)):
                strategy=load_strategy(module,request['params'])
                request={**request,'params':asdict(strategy.params)}
                self.assertEqual(data_bus_check(root, adapter, snapshot, request,
                                                strategy.requirements()),[])
                result=kernel_result(root, snapshot, {**request, 'run_id': str(index)}, strategy,
                                     loader=lambda _name, _params, strategy=strategy: strategy)
                results.append(result)
                self.assertTrue(result['trades'])
                self.assertEqual(len(result['equity']),3)
                self.assertFalse((root/'runs_parameters').exists())
            self.assertEqual(results[0]['data_hash'],results[1]['data_hash'])
            self.assertEqual(results[0]['execution_hash'],results[1]['execution_hash'])
            self.assertNotEqual(results[0]['metrics']['final_equity'],results[1]['metrics']['final_equity'])

    def test_parameter_change_uses_same_account_policy(self):
        from dataclasses import asdict
        from history_data import HistoricalLake
        from strategy import load_strategy
        from tests.history_fixture import make_snapshot
        module='plugins.strategies.breadth_low_amount_reversal'
        a={'module':module,'params':{'positions':4},'initial_cash':100000,'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root))
            strategy=load_strategy(module,{'positions':8})
            issue=data_bus_check(root, adapter, adapter.config,
                                 {**a,'params':asdict(strategy.params)}, strategy.requirements())
            self.assertEqual(issue,[])

    def test_long_history_stays_bounded_and_trace_does_not_duplicate_history(self):
        from dataclasses import asdict
        from datetime import date
        from history_data import HistoricalLake
        from strategy import load_strategy
        from tests.history_fixture import make_snapshot
        a={'module':'plugins.strategies.breadth_low_amount_reversal','params':{'positions':4},'initial_cash':100000,'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp)
            strategy=load_strategy(a['module'],a['params']); sizes=[]; generate=strategy.generate
            def observe(frame,account):
                sizes.append(max(map(len,frame.history.values())))
                return generate(frame,account)
            strategy.generate=observe
            snapshot=make_snapshot(root,history_start=date(2018,1,1))
            result=kernel_result(root, snapshot,
                                 {**a,'params':asdict(strategy.params),'run_id':'long'}, strategy,
                                 loader=lambda _name, _params: strategy)
            self.assertGreater(len(sizes),500)
            self.assertLessEqual(max(sizes),strategy.requirements().history_bars)
            trace=result['trace']
            self.assertTrue(all(frame['history']=={} for frame in trace['frames']))

    def test_market_source_cannot_be_excluded_using_account_role(self):
        import json
        from dataclasses import asdict
        from history_data import HistoricalLake
        from strategy import load_strategy
        from tests.history_fixture import make_snapshot
        a={'module':'plugins.strategies.breadth_low_amount_reversal','params':{'positions':4},'initial_cash':100000,'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root))
            manifest_path=root/'data/sources/fixture/manifest.json'; manifest=json.loads(manifest_path.read_bytes())
            manifest['files'][0]['role']='account'; manifest_path.write_text(json.dumps(manifest))
            strategy=load_strategy(a['module'],a['params'])
            issues=data_bus_check(root, adapter, adapter.config,
                                  {**a,'params':asdict(strategy.params)}, strategy.requirements())
            self.assertIn('incorrectly marked as account',issues[0]['message'])

    def test_unseen_strategy_requests_subset_and_only_declared_fields(self):
        from datetime import date
        from history_data import HistoricalLake
        from protocal import DataRequest, TargetSet
        from tests.history_fixture import make_snapshot
        class UnseenStrategy:
            strategy_id='unseen'
            from dataclasses import make_dataclass
            params=make_dataclass('Params',[],frozen=True)()
            def requirements(self): return DataRequest(('signal_close',),17,symbols=('600003',))
            def generate(self,frame,account):
                assert set(frame.fields)=={'600003'}
                expected = {'open','high','low','close','volume','amount','signal_close'}
                assert all(set(row.fields)==expected for row in frame.history['600003'])
                assert set(frame.fields['600003'])==expected
                assert len(frame.history['600003'])<=17
                return TargetSet(self.strategy_id,frame.at,{'600003':.1},{'600003':'test'}) if frame.context['trading'] else None
        request={'module':'unknown','params':{},'initial_cash':100000,'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root))
            result=kernel_result(root, adapter.config, {**request,'run_id':'new-strategy'}, UnseenStrategy(),
                                 loader=lambda _name, _params: UnseenStrategy())
            self.assertEqual(result['scope']['symbols'],['600003'])
            self.assertTrue(result['trades'])

    def test_declared_arbitrary_numeric_field_is_aligned_and_not_substituted(self):
        from history_data import HistoricalLake
        from protocal import DataRequest
        from tests.history_fixture import make_snapshot
        import pyarrow as pa
        import pyarrow.parquet as pq
        import json, hashlib
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root))
            path=root/'data/sources/fixture/daily/bars.parquet'; table=pq.read_table(path)
            table=table.append_column('arbitrary_value',pa.array([i+0.5 for i in range(len(table))]))
            pq.write_table(table,path)
            manifest_path=root/'data/sources/fixture/manifest.json'; manifest=json.loads(manifest_path.read_bytes())
            manifest['files'][0]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest['fields']={'quality':{'column':'arbitrary_value','unit':'研究分数'}}
            manifest_path.write_text(json.dumps(manifest))
            request={'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
            loaded=adapter._load(request,DataRequest(('quality',),17,symbols=('600003',)))
            values=loaded[-2]['quality']
            self.assertEqual(values.shape,(1,len(loaded[0].calendar)))
            self.assertEqual(values[0,0],3*len(loaded[0].calendar)+.5)
            self.assertEqual(data_bus_check(root, adapter, adapter.config, request,
                                            DataRequest(('absent',),17))[0]['code'],'historical_input_invalid')

    def test_market_universe_requirement_rejects_fixed_dataset_and_reserved_fields(self):
        from history_data import HistoricalLake
        from protocal import DataRequest
        from tests.history_fixture import make_snapshot
        import json
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            from protocal import DataBus
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root))
            path=root/'data/sources/fixture/manifest.json'; manifest=json.loads(path.read_bytes())
            manifest['scope']['universe']='fixed_symbols'; path.write_text(json.dumps(manifest))
            request={'dataset_id':'fixture','start':'2020-01-31','end':'2020-02-04'}
            issue=data_bus_check(root, adapter, adapter.config, request,
                                 DataRequest(('signal_close',),17,universe='CN_A_SHARE'))
            self.assertIn('universe_scope_mismatch',issue[0]['message'])
            manifest['fields']={'signal_close':{'column':'close','unit':'元'}}; path.write_text(json.dumps(manifest))
            issue=data_bus_check(root, adapter, adapter.config, request,
                                 DataRequest(('signal_close',),17))
            self.assertIn('reserved',issue[0]['message'])

    def test_execution_history_requirement_is_checked_before_short_strategy_run(self):
        from datetime import date
        from history_data import HistoricalLake
        from protocal import DataRequest
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as temp:
            from storage import DataStore
            root=Path(temp); bus=DataBus(DataStore(root)); adapter=HistoricalLake(bus,make_snapshot(root,history_start=date(2020,1,1)))
            issue=data_bus_check(root, adapter, adapter.config,
                                 {'dataset_id':'fixture','start':'2020-01-10','end':'2020-02-04'},
                                 DataRequest(('signal_close',),2))
            self.assertIn('requires 20',issue[0]['message'])
