import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from protocal import DataBus, DataError, Modules, Protocol
from storage import DataStore


class CollectionTests(unittest.TestCase):
    def test_minute_quotes_use_time_keys_and_minute_fields(self):
        from collect.quotes import collect_daily
        from types import SimpleNamespace
        names = ['date', 'time', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adjustflag']
        class Rows:
            error_code = '0'; error_msg = ''; fields = names
            def next(self): return False
        query = []
        def collect(*args, **kwargs):
            query.append(args[1])
            return Rows()
        provider = SimpleNamespace(login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
                                   query_history_k_data_plus=collect)
        with patch.dict('sys.modules', {'baostock': provider}):
            collect_daily({'codes': ['sh.600000'], 'start': '2026-10-01', 'end': '2026-10-02', 'frequency': '5'})
        self.assertEqual(query, [','.join(names)])

    def test_rejecting_the_bus_blocks_collection_without_private_files(self):
        from collect.data import Store
        from collect.display import CollectPort
        from unittest.mock import Mock
        ui = importlib.import_module('frontend.ui-protocol')
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            provider = Mock()
            port = CollectPort([], Store(bus), {'quotes': provider})
            backend = Protocol(Modules(port, port, port, port, port))
            request = ui.UiRequest('denied', 'workbench.sync', 'urn:lianghua:ui:input:workbench.sync:v1',
                                   {'id': 'quotes', 'input': {'codes': ['1.600000'], 'start': '2026-10-01', 'end': '2026-10-01'}})
            with patch.object(bus, 'write', side_effect=DataError('io_failed', 'denied')):
                message = asyncio.run(backend.send(request))[0]
            self.assertEqual(message.kind, 'problem')
            self.assertEqual(message.data['code'], 'io_failed')
            provider.collect.assert_not_called()
            self.assertEqual(list(Path(root).rglob('*')), [])
            backend.close()

    def test_failed_latest_write_does_not_publish_or_lock_source(self):
        from collect.data import Store
        from collect.core import SyncService
        from collect.quotes import EastmoneyQuotes
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            store = Store(bus)
            service = SyncService(store, {'quotes': EastmoneyQuotes({}, lambda *a: fixture['response'])})
            before = service.run('quotes', fixture['request'])
            write = bus.write
            def fail(kind, identity, name, value):
                if identity == 'latest':
                    raise DataError('io_failed', 'pointer failed')
                return write(kind, identity, name, value)
            with patch.object(bus, 'write', side_effect=fail):
                failed = service.run('quotes', fixture['request'])
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(store.latest('quotes')['snapshot_id'], before['snapshot_id'])
            self.assertEqual(service.active, set())

    def test_quote_values_must_be_finite_numbers(self):
        from collect.quotes import EastmoneyQuotes
        from collect.data import CollectionError
        import copy
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        for field, value in (('f2', '-'), ('f3', float('nan')), ('f5', -1), ('f6', True), ('f124', False)):
            response = copy.deepcopy(fixture['response'])
            response['data']['diff'][0][field] = value
            with self.subTest(field=field), self.assertRaises(CollectionError):
                EastmoneyQuotes({}, lambda *a: response).collect(fixture['request'])

    def test_security_lists_return_evidence_without_own_files(self):
        from collect.securities import Securities
        from types import SimpleNamespace
        import pandas as pd
        provider = SimpleNamespace(
            stock_info_sh_delist=lambda **_: pd.DataFrame([{'公司代码': '600001', '公司简称': '甲', '上市日期': '2000-01-01', '暂停上市日期': '2026-10-01'}]),
            stock_info_sz_delist=lambda **_: pd.DataFrame([{'证券代码': '000001', '证券简称': '乙', '上市日期': '2001-01-01', '终止上市日期': '2026-10-02'}]))
        result = Securities({}, provider).collect({'start': '2026-10-01', 'end': '2026-10-01', 'codes': []})
        self.assertEqual([row['bs_code'] for row in result['records']], ['sh.600001'])
        self.assertEqual(len(result['raw']), 2)

    def test_news_uses_shared_snapshot_storage(self):
        from collect.news import News
        from collect.core import SyncService
        from collect.data import Store
        page = '<article class="content-section" id="section-1"><h2>标题</h2><div class="content-body"><p>正文</p></div></article>'
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            store = Store(bus)
            service = SyncService(store, {'news': News({}, lambda url: page)})
            job = service.run('news', {'start': '2026-10-01', 'end': '2026-10-01', 'codes': []})
            self.assertEqual(job['status'], 'succeeded', job['error'])
            snapshot = store.get_snapshot(job['snapshot_id'])
            self.assertEqual(snapshot['records'][0]['content'], '正文')
            self.assertEqual(snapshot['raw'][0]['response'], page)
            self.assertEqual(list(Path(root).rglob('*.db')), [])

    def test_latest_quote_fields_are_selectable(self):
        from collect.quotes import EastmoneyQuotes
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        result = EastmoneyQuotes({}, lambda *a: fixture['response']).collect({**fixture['request'], 'fields': ['price', 'volume']})
        self.assertEqual(set(result['records'][0]), {'code', 'data_at', 'raw_page', 'price', 'volume'})

    def test_latest_quote_rejects_unsupported_parameters(self):
        from collect.quotes import EastmoneyQuotes
        from collect.data import CollectionError
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        from unittest.mock import Mock
        transport = Mock(return_value=fixture['response'])
        provider = EastmoneyQuotes({}, transport)
        for extra in ({'fields': []}, {'fields': ['unknown']}, {'frequency': 'w'}, {'adjustment': '2'}):
            with self.subTest(extra=extra), self.assertRaises(CollectionError):
                provider.collect({**fixture['request'], **extra})
        transport.assert_not_called()

    def test_lake_schedule_uses_the_same_protocol_store(self):
        from collect.lake_display import LakePort
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            port = LakePort(bus)
            port.handle('lake.setContinuousFetch', {'enabled': False})
            value = json.loads(bus.read('state', 'schedules', 'lake.json'))
            self.assertFalse(value['enabled'])
            self.assertFalse((Path(root) / 'data/lake').exists())
            port.close()
    def test_ui_manual_collection_schedule_and_restart_use_protocol(self):
        from collect.data import Store
        from collect.display import CollectPort
        from collect.quotes import EastmoneyQuotes
        ui = importlib.import_module('frontend.ui-protocol')
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        source = {'id': 'quotes', 'name': '报价', 'frequency': '最新', 'defaults': {'codes': ['1.600000']}}
        class Unused:
            def handle(self, *args): raise AssertionError('wrong module')
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            port = CollectPort([source], Store(bus), {'quotes': EastmoneyQuotes({}, lambda *a: fixture['response'])})
            backend = Protocol(Modules(Unused(), Unused(), port, Unused(), Unused()))
            def send(op, value):
                request = ui.UiRequest('interaction', op, f'urn:lianghua:ui:input:{op}:v1', value)
                message = asyncio.run(backend.send(request))[0]
                self.assertNotEqual(message.kind, 'problem', message.data)
                return message
            message = send('workbench.sync', {'id': 'quotes', 'input': fixture['request']})
            port.wait()
            job = send('workbench.job', {'id': message.data['job_id']}).data
            self.assertEqual(job['status'], 'succeeded')
            page = send('workbench.snapshot', {'id': job['snapshot_id']}).data
            self.assertEqual(page['records'][0]['price'], 10.12)
            schedule = send('workbench.schedule', {'id': 'quotes', 'enabled': True, 'interval_seconds': 120, 'input': fixture['request']}).data
            self.assertEqual(schedule['request']['codes'], ['1.600000'])
            restarted = Store(DataBus(DataStore(Path(root))))
            self.assertEqual(restarted.read('schedules', 'quotes')['interval_seconds'], 120)
            self.assertEqual(restarted.get_snapshot(job['snapshot_id'])['raw'][0]['response'], fixture['response'])
            port.scheduler.tick(at=schedule['next_at'], local_date='2026-10-12')
            port.wait()
            scheduled_id = restarted.read('source_jobs', 'quotes')['job_id']
            scheduled_job = send('workbench.job', {'id': scheduled_id}).data
            self.assertEqual(scheduled_job['status'], 'succeeded')
            self.assertEqual(scheduled_job['request']['end'], '2026-10-12')
            self.assertNotEqual(scheduled_id, job['job_id'])
            backend.close()
            port.scheduler.tick(at=schedule['next_at'] + 120, local_date='2026-10-13')
            self.assertEqual(restarted.read('source_jobs', 'quotes')['job_id'], scheduled_id)

    def test_display_rejects_invalid_manual_and_automatic_parameters(self):
        from collect.data import Store
        from collect.display import CollectPort
        with tempfile.TemporaryDirectory() as root:
            port = CollectPort([], Store(DataBus(DataStore(Path(root)))), {})
            with self.assertRaises(ValueError):
                port.handle('workbench.sync', {'id': 'quotes', 'input': {'codes': ['x', 'x'], 'start': '2026-10-02', 'end': '2026-10-01'}})
            with self.assertRaises(ValueError):
                port.handle('workbench.schedule', {'id': 'quotes', 'enabled': True, 'interval_seconds': 1, 'input': {'codes': [], 'start': '2026-10-01', 'end': '2026-10-02'}})

    def test_quotes_and_snapshot_match_frozen_old_result(self):
        from collect.data import Store
        from collect.quotes import EastmoneyQuotes
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            store = Store(bus, clock=lambda: '2026-09-30T00:00:00+00:00')
            result = EastmoneyQuotes({}, lambda *a: fixture['response']).collect(fixture['request'])
            snapshot = store.snapshot('quotes', result, fixture['request'])
            self.assertEqual(snapshot, fixture['snapshot'])
            self.assertEqual(store.latest('quotes'), snapshot)

    def test_failed_collection_preserves_evidence_and_old_snapshot(self):
        from collect.data import Store
        from collect.core import SyncService
        from collect.quotes import EastmoneyQuotes
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        with tempfile.TemporaryDirectory() as root:
            store = Store(DataBus(DataStore(Path(root))))
            good = EastmoneyQuotes({}, lambda *a: fixture['response'])
            service = SyncService(store, {'quotes': good})
            before = service.run('quotes', fixture['request'])
            service.providers['quotes'] = EastmoneyQuotes({}, lambda *a: {'data': {'diff': []}})
            failed = service.run('quotes', fixture['request'])
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(store.latest('quotes')['snapshot_id'], before['snapshot_id'])
            self.assertEqual(store.read('failed_evidence', failed['job_id'])['raw'][0]['response'], {'data': {'diff': []}})

    def test_storage_failure_releases_source_lock(self):
        from collect.data import Store
        from collect.core import SyncService
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); store = Store(bus)
            service = SyncService(store, {'quotes': object()})
            with patch.object(bus, 'write', side_effect=DataError('io_failed', 'disk full')):
                with self.assertRaises(DataError):
                    service.begin('quotes', {})
            self.assertNotIn('quotes', service.active)
            self.assertEqual(service.begin('quotes', {})['status'], 'running')

    def test_corrupt_records_are_not_treated_as_absent(self):
        from collect.data import Store
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            bus.write('state', 'jobs', 'j.json', b'not-json')
            with self.assertRaises(ValueError):
                Store(bus).read('jobs', 'j')

    def test_daily_parameters_reach_the_provider(self):
        from collect.quotes import collect_daily
        from types import SimpleNamespace
        calls=[]
        class Rows:
            error_code='0'; error_msg=''; fields=['date','code','open','high','low','close','volume','amount']
            def next(self): return False
        provider=SimpleNamespace(login=lambda:SimpleNamespace(error_code='0'), logout=lambda:None,
            query_history_k_data_plus=lambda *args,**kwargs: calls.append((args,kwargs)) or Rows())
        with patch.dict('sys.modules', {'baostock':provider}):
            collect_daily({'codes':['sh.600000'],'start':'2026-09-01','end':'2026-09-30',
                           'frequency':'w','adjustment':'2','fields':Rows.fields})
        self.assertEqual(calls[0][1]['frequency'], 'w')
        self.assertEqual(calls[0][1]['adjustflag'], '2')
        self.assertEqual(calls[0][0][1], ','.join(Rows.fields))

    def test_daily_can_collect_status_without_price_fields(self):
        from collect.quotes import collect_daily
        from types import SimpleNamespace
        class Rows:
            error_code = '0'; fields = ['date', 'code', 'isST', 'volume', 'tradestatus']
            def next(self): return False
        provider = SimpleNamespace(login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
                                   query_history_k_data_plus=lambda *args, **kwargs: Rows())
        with patch.dict('sys.modules', {'baostock': provider}):
            result = collect_daily({'codes': ['sz.301001'], 'start': '2026-10-01', 'end': '2026-10-02', 'fields': Rows.fields})
        self.assertEqual(result['records'], [])
