from market.views import Views
import asyncio
import importlib
import json
import tempfile
import unittest
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from market import DataBus
from protocol import DataError
from protocol import TableDef, TableQuery
from backend import Modules, Protocol
from storage import DataStore
from tests.history_fixture import fixture_data
from collect.methods import Methods


def _calendar(sessions):
    days = sorted(set(sessions))
    return {"sessions": days,
            "coverage": ([{"start": days[0], "end": days[-1]}] if days else [])}


def _bind_rows(bus, identity, kind, rows, keys, *, types=None):
    from market import table_name

    table = table_name(f'{identity}/{kind}')
    bus.put_rows(table, rows, keys=keys, types=types)
    reference = 'lake:' + table
    bus.bind(identity, kind, reference)
    return reference


def _daily_row(day, code='600000', close=10.5):
    return {'code': code, 'date': day, 'open': close, 'high': close + 1,
            'low': close - 1, 'close': close, 'volume': 100.0,
            'amount': 100 * close, 'tradestatus': 1.0}


class CollectionTests(unittest.TestCase):
    def test_task_recovery_and_schedule_queries_do_not_read_raw_logs(self):
        from collect.core import Collect
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            store = DataStore(Path(root))
            bus = DataBus(store)
            sources = Sources(bus)
            source = {"id": "fixture", "name": "Fixture", "method": "fixture",
                      "frequency": "daily", "defaults": {}}
            sources.register(source)
            job = {"job_id": "one", "source_id": "fixture", "status": "finished",
                   "outcome": "succeeded", "finished_at": "2026-10-01T12:00:00"}
            schedule = {"source_id": "fixture", "enabled": False, "next_at": None}
            bus.define(TableDef('collection_jobs', {'job_id': 'VARCHAR', 'settings': 'JSON'}, ('job_id',), ('settings',)))
            bus.define(TableDef('collection_schedules', {'source_id': 'VARCHAR', 'settings': 'JSON'}, ('source_id',), ('settings',)))
            bus.put_rows('collection_jobs', [{'job_id': 'one', 'settings': json.dumps(job)}], keys=('job_id',))
            bus.put_rows('collection_schedules', [{'source_id': 'fixture', 'settings': json.dumps(schedule)}], keys=('source_id',))
            bus.define(TableDef('collection_audit', {'id': 'VARCHAR', 'settings': 'JSON'}, ('id',)))
            bus.put_rows('collection_audit', [{'id': 'audit', 'settings': '{"event":"old"}'}], keys=('id',))
            collect = Collect(sources, Methods(bus))
            self.assertEqual(collect.status("one"), job)
            collect.tick(at=1)
            view = collect.handle("collection.sources", {})[0]
            self.assertEqual(view["job"], job)
            self.assertEqual(view["schedule"], schedule)
            self.assertEqual(bus.scan(TableQuery('collection_audit')).table.num_rows, 1)
            collect.close()

    def test_baostock_spacing_and_network_retry(self):
        from collect.methods import BaostockDaily
        from types import SimpleNamespace
        from unittest.mock import Mock
        clock=[0.]
        def sleep(seconds): clock[0]+=seconds
        times=[]
        replies=iter(['10002007','0','0'])
        def query():
            times.append(clock[0])
            return SimpleNamespace(error_code=next(replies),error_msg='network')
        adapter=BaostockDaily({'params':{'interval':2,'retry_delay':10,'retries':1}})
        with patch.object(BaostockDaily,'_last_request',None), patch('collect.methods.time.monotonic',side_effect=lambda:clock[0]), patch('collect.methods.time.sleep',side_effect=sleep):
            self.assertEqual(adapter._call(query).error_code,'0')
            BaostockDaily({'params':{'interval':2}})._call(query)
        self.assertEqual(times,[0.,10.,12.])
        failed=Mock(return_value=SimpleNamespace(error_code='10002007',error_msg='network'))
        with patch.object(BaostockDaily,'_last_request',None),patch('collect.methods.time.sleep'):
            self.assertEqual(adapter._call(failed).error_code,'10002007')
        self.assertEqual(failed.call_count,2)
        for code in ('10001011','10004011'):
            call=Mock(return_value=SimpleNamespace(error_code=code,error_msg='denied'))
            with patch.object(BaostockDaily,'_last_request',None),patch('collect.methods.time.sleep') as wait:
                self.assertEqual(adapter._call(call).error_code,code)
                call.assert_called_once();wait.assert_not_called()

    def test_methods_merge_unique_current_records_without_batch_files(self):
        from collect.methods import Methods
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); methods = Methods(bus)
            source = {'id': 'custom', 'method': 'fixture', 'params': {'key_fields': ['identity']}}
            from collect.sources import Sources
            sources = Sources(bus)
            sources.register({**source, 'name': 'Custom', 'frequency': 'manual', 'defaults': {}})
            source = sources.get('custom')
            packet = {'records': [{'identity': 'a', 'value': 1}], 'raw': [], 'scope': {}}
            with self.assertRaises(DataError) as caught:
                methods.publish({**source, 'params': {}}, packet)
            self.assertEqual(caught.exception.code, 'identity_policy')
            methods.publish(source, packet)
            methods.publish(source, packet)
            view = methods.records(source, 0, 50)
            self.assertEqual(view['total'], 1)
            self.assertEqual(view['records'], packet['records'])
            reference = bus.source('custom')
            before = bus.table(reference)
            with self.assertRaises(DataError):
                methods.publish(source, {**packet, 'records': [{'identity': 'a', 'value': 2}]})
            self.assertTrue(bus.table(reference).equals(before))

    def test_collect_request_normalization_is_shared_and_rejects_before_fetch(self):
        from collect.methods import Methods

        source = {
            "id": "fixture", "name": "Fixture", "method": "fixture",
            "frequency": "daily", "data_type": "daily", "modes": ["auto", "fix"],
            "fields": ["date", "code", "open", "high", "low", "close", "volume",
                        "amount", "turn", "pctChg", "tradestatus", "isST", "peTTM",
                        "pbMRQ", "psTTM", "pcfNcfTTM"],
            "defaults": {"codes": ["sh.600036", "sz.000001"],
                         "fields": ["date", "code", "close"],
                         "provider_options": {"page_size": 100}},
        }
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            self.addCleanup(bus.close)
            self._check_collect_request_normalization(Methods(bus), source)

    def _check_collect_request_normalization(self, methods, source):
        request = {"codes": " sh.600036, sz.000001, sh.000300, sh.600036 ",
                   "fields": "date, code, close, amount, close",
                   "start": "2026-10-01", "end": "2026-10-13",
                   "provider_options": {"page_size": 50}}
        auto = methods.prepare_request(source, {**request, "mode": "auto"})
        manual = methods.prepare_request(source, {**request, "mode": "fix"})
        for key in ("codes", "fields", "start", "end", "provider_options"):
            self.assertEqual(auto[key], manual[key])
        self.assertEqual(auto["codes"], ["sh.600036", "sz.000001", "sh.000300"])
        self.assertEqual(auto["fields"], ["close", "amount"])
        self.assertEqual(auto["provider_options"], {"page_size": 50})
        basic = methods.prepare_request(source, {"fields": "basic"})
        full = methods.prepare_request(source, {"fields": "full"})
        self.assertEqual(basic["fields"], ["open", "high", "low", "close", "volume",
                                            "amount", "turn", "pctChg", "tradestatus", "isST"])
        self.assertEqual(full["fields"], ["open", "high", "low", "close", "volume",
                                           "amount", "turn", "pctChg", "tradestatus", "isST",
                                           "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"])

        for invalid in (
            {"codes": " , ", "start": "2026-10-01", "end": "2026-10-13"},
            {"codes": ["sh.600036", ""], "start": "2026-10-01", "end": "2026-10-13"},
            {"fields": ",", "start": "2026-10-01", "end": "2026-10-13"},
            {"codes": "sh.600036", "start": "2026-10-14", "end": "2026-10-13"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                methods.prepare_request(source, invalid)

        with tempfile.TemporaryDirectory() as root:
            calls = []
            bus = DataBus(DataStore(Path(root)))
            from collect.core import Collect
            from collect.sources import Sources
            sources = Sources(bus)
            sources.register({**source, "defaults": {"codes": ["sh.600036"]}})
            methods = Methods(bus, {"fixture": lambda _source, _request: calls.append(True)})
            collect = Collect(sources, methods)
            with self.assertRaises(ValueError):
                collect.handle("collection.start", {"source_id": "fixture", "codes": " , "})
            self.assertEqual(calls, [])
            self.assertIsNone(collect._fetch_thread)

    def test_daily_source_summary_and_page_do_not_build_a_backtest_dataset(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root))
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily", "default": True,
                              "modes": ["query"], "dataset_id": "fixture", "storage_id": "fixture",
                              "defaults": {}})
            collect = Collect(sources, Methods(bus))
            with patch.object(bus, "query", side_effect=AssertionError("no backtest query")):
                views = collect.handle("collection.sources", {})
                page = collect.handle("collection.snapshot", {
                    "id": "daily", "offset": 50, "limit": 10,
                    "start": "2020-01-02", "end": "2020-02-04",
                    "symbols": ["600000", "600001", "600002", "600003"],
                })
            self.assertGreater(page["total"], 50)
            self.assertEqual(len(page["records"]), 10)
            self.assertTrue(all(row['date'] >= '2020-01-02' for row in page['records']))
            self.assertEqual({row['code'] for row in page['records']},
                             {'sh.600000', 'sh.600001', 'sh.600002', 'sh.600003'})
            self.assertTrue(views[0]["snapshot"]["snapshot_id"])
            self.assertGreater(views[0]["records"], page["total"])

    def test_full_fields_require_declared_provider_capability_and_packet_uses_request_metadata(self):
        from collect.methods import Methods

        source = {"id": "daily", "method": "fixture", "data_type": "daily",
                  "frequency": "manual", "fields": ["open", "close"],
                  "defaults": {}}
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {"fixture": lambda _source, _request: {
                "records": [], "raw": [], "scope": {"codes": []}}})
            with self.assertRaisesRegex(ValueError, "full"):
                methods.prepare_request(source, {"fields": "full"})
            packet = methods.fetch(source, methods.prepare_request(
                source, {"frequency": "d", "adjustment": "3", "fields": "basic"}))
        self.assertEqual(packet["frequency"], "d")
        self.assertEqual(packet["adjustment"], "3")
        self.assertEqual(packet["data_type"], "daily")

    def test_daily_source_summary_applies_registered_code_scope(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root)); sources = Sources(bus)
            sources.register({"id": "benchmark", "name": "Benchmark", "method": "fixture",
                              "frequency": "daily", "data_type": "daily", "modes": ["query"],
                              "defaults": {"codes": ["sh.600003"]}, "dataset_id": "fixture",
                              "storage_id": "fixture"})
            with patch.object(bus, "query", side_effect=AssertionError("no backtest query")):
                view = Collect(sources, Methods(bus)).handle("collection.sources", {})[0]
            self.assertEqual(view["records"], 157)
            self.assertEqual(view["snapshot"]["scope"]["symbols"], ["sh.600003"])

    def test_daily_page_matches_only_normalized_security_aliases(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            symbols = ['000001', 'sh.000001', 'sz.000001']
            reference = _bind_rows(bus, 'fixture', 'bars',
                [_daily_row('2020-01-02', code) for code in symbols], ('code', 'date'))
            bus.save_calendar('fixture', _calendar(['2020-01-02', '2020-01-03']))
            bus.save_manifest('fixture', {'schema': 3, 'fields': {},
                'inputs': {'bars': reference}, 'scope': {'symbols': symbols, 'universe': 'market'}})
            sources = Sources(bus)
            methods = Methods(bus)
            for source_id, code, expected in (
                ('index', 'sh.000001', ['sh.000001']),
                ('bank', '000001', ['000001', 'sz.000001']),
            ):
                with self.subTest(source=source_id):
                    source = sources.register({'id': source_id, 'name': source_id,
                        'method': 'fixture', 'frequency': 'daily', 'data_type': 'daily',
                        'dataset_id': 'fixture', 'storage_id': 'fixture', 'defaults': {'codes': [code]}})
                    summary = methods.daily_collection_summary(source, 'fixture')
                    page = methods.records(source, limit=10)
                    self.assertEqual(summary['records_count'], len(expected))
                    self.assertEqual(page['total'], len(expected))
                    self.assertEqual([row['code'] for row in page['records']], expected)

    def test_daily_source_calendar_is_loaded_and_fix_enters_source(self):
        from collect.core import Collect
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            calendar = _calendar(['2026-01-02', '2026-01-05', '2026-01-06'])
            bus.save_calendar('cn_a', calendar)
            sources = Sources(bus)
            sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                'frequency': 'daily', 'data_type': 'daily', 'modes': ['fix'],
                'storage_id': 'cn_a', 'dataset_id': 'cn_a', 'calendar': 'calendar:cn_a',
                'price_basis': 'raw', 'defaults': {}})
            row = _daily_row('2026-01-05', '600036')
            methods = Methods(bus, {'fixture': lambda _source, _request: {
                'records': [row], 'raw': [], 'scope': {'universe': 'market'}}})
            collect = Collect(sources, methods)
            self.addCleanup(collect.close)
            result = collect.handle('collection.start', {
                'source_id': 'daily', 'mode': 'fix', 'codes': ['sh.600036'],
                'start': '2026-01-05', 'end': '2026-01-05'})
            self.assertTrue(result['ok'], result)
            collect._fetch_thread.join(2)
            self.assertEqual(collect.handle('collection.current', {})['status'], 'awaiting_publish')
            self.assertTrue(collect.handle('collection.merge', {'dryRun': False})['ok'])
            collect._merge_thread.join(2)
            table = bus.table(bus.source('cn_a', 'bars'), columns=tuple(row))
            self.assertEqual(table.to_pylist(), [row])
            self.assertEqual(bus.calendar('cn_a'), calendar)

    def test_string_daily_provider_rows_close_the_standard_publish_query_loop(self):
        import sys
        from datetime import date
        from protocol import DataQuery
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            calendar = _calendar(["2026-01-02", "2026-01-05", "2026-01-06"])
            bus.save_calendar('cn_a', calendar)
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "daily",
                              "frequency": "manual", "data_type": "daily",
                              "modes": ["fix"], "default": True,
                              "storage_id": "cn_a", "dataset_id": "cn_a",
                              "calendar": "calendar.json", "price_basis": "raw",
                              "defaults": {}})
            fields = ["date", "code", "open", "high", "low", "close", "volume",
                      "amount", "turn", "pctChg", "tradestatus", "isST"]
            trade_dates = ["calendar_date", "is_trading_day"]
            class Rows:
                error_code = "0"
                error_msg = ""
                def __init__(self):
                    self.rows = [["2026-01-07", "sh.600036", "10.0", "11.0", "9.0",
                                  "10.5", "100", "1050", "1.2", "0.5", "1", "0"]]
                def next(self):
                    return bool(self.rows)
                def get_row_data(self):
                    return self.rows.pop(0)
            Rows.fields = fields
            class TradeDates:
                error_code = "0"
                error_msg = ""
                fields = trade_dates
                def __init__(self, start, end):
                    from datetime import date, timedelta
                    first, last = date.fromisoformat(start), date.fromisoformat(end)
                    self.rows = [[(first + timedelta(days=offset)).isoformat(), "1"]
                                 for offset in range((last - first).days + 1)]
                def next(self):
                    return bool(self.rows)
                def get_row_data(self):
                    return self.rows.pop(0)
            provider = SimpleNamespace(
                login=lambda: SimpleNamespace(error_code="0", error_msg=""),
                logout=lambda: None,
                query_trade_dates=lambda *args, **kwargs: TradeDates(kwargs["start_date"], kwargs["end_date"]),
                query_history_k_data_plus=lambda *args, **kwargs: Rows())
            methods = Methods(bus)
            collect = Collect(sources, methods)
            with patch.dict(sys.modules, {"baostock": provider}), patch('collect.methods.time.sleep'):
                fetched = collect.handle("collection.start", {
                    "source_id": "daily", "codes": "600036", "fields": "basic",
                    "start": "2026-01-07", "end": "2026-01-07",
                    "calendar_end": "2026-01-08"})
                self.assertTrue(fetched["ok"])
                collect._fetch_thread.join(2)
                self.assertEqual(collect.handle("collection.current", {})["status"], "awaiting_publish")
                packet = collect._packets[collect._current_job_id]
                history = next(item for item in packet["raw"]
                               if item["response"]["fields"][0] == "date")
                self.assertEqual(history["response"]["rows"][0][2], "10.0")
                self.assertEqual(packet["calendar"], {
                    "sessions": ["2026-01-02", "2026-01-05", "2026-01-06",
                                 "2026-01-07", "2026-01-08"],
                    "coverage": [{"start": "2026-01-02", "end": "2026-01-08"}],
                })
                self.assertEqual(packet["records"][0]["open"], 10.0)
                self.assertIsInstance(packet["records"][0]["close"], float)
                self.assertTrue(collect.handle("collection.merge", {"dryRun": True})["ok"])
                collect._merge_thread.join(2)
                self.assertIsNone(collect.handle("collection.merge_job", {})["error"])
                self.assertTrue(collect.handle("collection.merge", {"dryRun": False})["ok"])
                collect._merge_thread.join(2)
                self.assertIsNone(collect.handle("collection.merge_job", {})["error"])
            bus.save_manifest('cn_a', {'schema': 3, 'fields': {},
                'calendar': 'calendar:cn_a', 'scope': {'symbols': ['sh.600036'], 'universe': 'market'},
                'inputs': {'bars': bus.source('cn_a', 'bars')}})
            dataset = bus.query(DataQuery(
                "cn_a", date(2026, 1, 7), date(2026, 1, 7),
                ("open", "high", "low", "close", "volume", "amount"), 1, "raw",
                symbols=("sh.600036",), universe="market"))
            values = dataset.records[0].frame.fields["sh.600036"]
            self.assertEqual(values["close"], 10.5)

    def test_data_bus_rejects_noncanonical_daily_packets_before_any_write(self):
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            row = {"code": "600036", "date": "2026-01-02", "open": 10.0,
                   "high": 11.0, "low": 9.0, "close": 10.5,
                   "volume": 1.0, "amount": 2.0}
            for request in (
                {"frequency": "m", "price_basis": "raw"},
                {"frequency": "daily", "price_basis": "adjusted"},
                {"frequency": "daily", "adjustment": "2"},
            ):
                with self.subTest(request=request), self.assertRaises(DataError) as caught:
                    Methods(bus).publish({'id': 'fixture', 'data_type': 'daily',
                                          'storage_id': 'cn_a', 'dataset_id': 'fixture'}, {
                        "records": [row], "calendar": _calendar(["2026-01-02"]),
                        "raw": [],
                        "request": request, "scope": {"universe": "market"},
                    })
                self.assertEqual(caught.exception.code, "canonical_daily")
            with self.assertRaises(DataError) as caught:
                bus.source('cn_a', 'bars')
            self.assertEqual(caught.exception.code, 'missing')

    def test_source_query_keeps_frontend_security_search_shape(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            _bind_rows(bus, 'stocks', 'records', [{
                'code': 'sh.600036', 'code_name': '招商银行', 'status': '1',
                'ipoDate': '2002-04-09', 'outDate': '',
            }], ('code',))
            _bind_rows(bus, 'daily', 'bars', [{
                **_daily_row('2026-09-11', 'sh.600036', 42.5),
                'pctChg': 1.25, 'amount': 123456789.0,
            }], ('code', 'date'))
            sources = Sources(bus)
            for name, data_type in [('stocks', 'stock_info'), ('daily', 'daily')]:
                sources.register({'id': name, 'name': name, 'method': 'fixture',
                                  'frequency': 'daily', 'data_type': data_type, 'defaults': {}})
            result = Views(bus, sources).handle('data.search', {'q': '招商'})
            self.assertEqual(result['items'][0]['代码'], 'sh.600036')
            self.assertEqual(result['items'][0]['名称'], '招商银行')
            self.assertEqual(result['items'][0]['最新收盘'], 42.5)
            self.assertEqual(result['items'][0]['涨跌幅'], 1.25)
            self.assertEqual(result['items'][0]['行情日期'], '2026-09-11')
            self.assertAlmostEqual(result['items'][0]['成交额亿'], 1.23456789)

    def test_source_kline_keeps_frontend_curve_shape(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            _bind_rows(bus, 'market', 'bars', [{
                **_daily_row('2026-01-02', 'sh.600036'),
                'open': 10.0, 'low': 9.0, 'high': 11.0,
            }], ('code', 'date'))
            bus.save_calendar('market', _calendar(['2026-01-02', '2026-01-05']))
            sources = Sources(bus)
            sources.register({'id': 'daily', 'name': 'Daily', 'method': 'fixture',
                'frequency': 'daily', 'data_type': 'daily', 'modes': ['kline'],
                'default': True, 'dataset_id': 'market', 'calendar': 'calendar:market', 'defaults': {}})
            with patch.object(bus, 'query', side_effect=AssertionError('no backtest query')):
                result = Views(bus, sources).handle('data.bars', {'code': '600036',
                    'freq': '日', 'adj': '不复权'})
            self.assertEqual(result['dates'], ['2026-01-02'])
            self.assertEqual(result['candles'], [[10.0, 10.5, 9.0, 11.0]])

    def test_source_fetch_and_merge_share_current_task_and_dry_run_does_not_publish(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily",
                              "modes": ["auto", "fix"], "default": True,
                              "defaults": {}})
            published = []
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"code": "600036", "date": "2026-01-02",
                              "open": 10.0, "high": 11.0, "low": 9.0,
                              "close": 10.5, "volume": 1.0, "amount": 2.0}],
                "raw": [], "scope": {"universe": "market"}}})
            methods.publish = lambda source, packet: published.append(packet)
            methods.prepare = lambda source, packet: {"records": len(packet["records"]), "incoming": len(packet["records"])}
            collect = Collect(sources, methods)
            fetched = collect.handle("collection.start", {
                "mode": "fix", "start": "2026-01-02", "end": "2026-01-02",
                "codes": "600036", "fields": "basic", "force": False,
                "sleep": 0.0, "max_tasks": 0,
            })
            self.assertTrue(fetched["ok"])
            self.assertEqual(published, [])
            collect._fetch_thread.join(2)
            self.assertEqual(collect.handle("collection.current", {} )["status"], "awaiting_publish")
            self.assertTrue(collect.handle("collection.merge", {"dryRun": True})["ok"])
            collect._merge_thread.join(2)
            self.assertEqual(published, [])
            self.assertTrue(collect.handle("collection.merge", {"dryRun": False})["ok"])
            collect._merge_thread.join(2)
            self.assertEqual(len(published), 1)

    def test_sources_concurrent_instances_merge_registry_updates(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            left, right = Sources(bus), Sources(bus)
            def descriptor(identity):
                return {"id": identity, "name": identity, "method": identity,
                        "frequency": "daily", "defaults": {}}
            errors = []
            threads = [
                threading.Thread(target=lambda: left.register(descriptor("left"))),
                threading.Thread(target=lambda: right.register(descriptor("right"))),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual({item["id"] for item in Sources(bus).list()},
                             {"left", "right"})

    def test_sources_protocol_returns_frontend_source_array_with_runtime_fields(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            collect = Collect(sources, Methods(bus, {}))
            result = collect.handle("collection.sources", {})
            self.assertIsInstance(result, list)
            self.assertEqual(set(result[0]), {
                "id", "name", "frequency", "defaults", "job", "schedule",
                "integrity_error", "snapshot", "records",
            })

    def test_source_changes_preserve_datasets_and_benchmarks(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            registry = {"sources": [], "datasets": [{"id": "fixture_dataset"}],
                        "benchmarks": [{"id": "hs300"}]}
            bus.update_registry(lambda current: {**current, **registry})
            sources = Sources(bus)
            descriptor = {"id": "fixture", "name": "Fixture", "method": "fixture",
                          "frequency": "daily", "defaults": {"codes": ["600001"]}}
            sources.register(descriptor)
            sources.update("fixture", {"lookback_days": 3})
            sources.remove("fixture")
            saved = bus.registry()
            self.assertEqual(saved["datasets"], registry["datasets"])
            self.assertEqual(saved["benchmarks"], registry["benchmarks"])
            self.assertEqual(saved["sources"], [])

    def test_sources_registers_dynamic_descriptors_through_databus(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            descriptor = {
                'id': 'fixture', 'name': 'Fixture', 'method': 'fixture',
                'frequency': 'daily', 'defaults': {'codes': ['600001']},
            }
            self.assertEqual(sources.register(descriptor), descriptor)
            with self.assertRaises(ValueError):
                sources.register(descriptor)
            restarted = Sources(DataBus(DataStore(Path(root))))
            self.assertEqual(restarted.get('fixture'), descriptor)

    def test_collection_records_use_the_registered_typed_source_table(self):
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {'fixture': lambda source, request: {
                'records': [{'code': 'sh.600001', 'date': '2026-11-01', 'close': 12.5}],
                'raw': [], 'scope': {'symbols': ['sh.600001']}}})
            source = {'id': '行情源', 'method': 'fixture',
                      'params': {'key_fields': ['code', 'date']}}
            packet = methods.fetch(source, {})
            methods.publish(source, packet)
            reference = bus.source('行情源', 'records')
            self.assertTrue(reference.startswith('lake:'))
            rows = bus.table(reference).to_pylist()
            self.assertEqual(rows, [{'code': 'sh.600001', 'date': '2026-11-01', 'close': 12.5}])

    def test_sources_accepts_registered_market_descriptor_fields(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            sources = Sources(DataBus(DataStore(Path(root))))
            descriptor = {
                "id": "market_daily", "name": "Market daily", "method": "daily",
                "frequency": "daily", "data_type": "daily", "provider": "fixture",
                "fields": ["open", "high", "low", "close", "volume", "amount"],
                "storage_id": "cn_a", "dataset_id": "market", "calendar": "market_calendar",
                "defaults": {"codes": ["sh.600000"]},
            }
            self.assertEqual(sources.register(descriptor), descriptor)

    def test_methods_instantiates_same_method_per_source_configuration(self):
        from collect.methods import Methods

        class Probe:
            def __init__(self, source):
                self.source = source
            def collect(self, request):
                return {"records": [{"value": self.source["defaults"]["value"]}],
                        "raw": [], "scope": {"source": self.source["id"]}}

        with tempfile.TemporaryDirectory() as root:
            methods = Methods(DataBus(DataStore(Path(root))), {"probe": Probe})
            left = {"id": "left", "method": "probe", "defaults": {"value": 1}}
            right = {"id": "right", "method": "probe", "defaults": {"value": 2}}
            self.assertEqual(methods.fetch(left, {})["records"][0]["value"], 1)
            self.assertEqual(methods.fetch(right, {})["records"][0]["value"], 2)

    def test_backend_starts_with_empty_registry_and_new_source_needs_no_rebuild(self):
        from backend import build_backend

        with tempfile.TemporaryDirectory() as root:
            backend = build_backend(Path(root))
            try:
                self.assertEqual(backend._modules.sources.collect.sources.list(), [])
                backend._modules.sources.collect.sources.register({
                    "id": "new_quotes", "name": "New quotes", "method": "quotes",
                    "frequency": "daily", "defaults": {"codes": ["1.600000"]},
                })
                with patch("collect.methods.EastmoneyQuotes.collect", return_value={
                        "records": [{"code": "1.600000", "data_at": "2026-10-14T15:00:00+08:00",
                                     "source": "new_quotes"}], "raw": [],
                        "scope": {"kind": "fixture"}}):
                    result = backend._modules.sources.handle("collection.run", {"source_id": "new_quotes"})
                self.assertEqual(result["outcome"], "succeeded")
            finally:
                backend.close()

    def test_methods_fetch_publish_and_log_use_databus_and_release_as_one_batch(self):
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {
                'fixture': lambda source, request: {
                    'records': [{'code': '600001', 'date': request['date'], 'close': 10.0, 'raw_page': 0}],
                    'raw': [{'source': source['id'], 'request': request}],
                    'data_as_of': request['date'],
                    'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
                },
            })
            source = {'id': 'fixture', 'method': 'fixture', 'params': {'key_fields': ['code', 'date']}}
            packet = methods.fetch(source, {'date': '2026-10-14'})
            published = methods.publish(source, packet)
            logged = methods.log({'source_id': 'fixture', 'event': 'published'})
            self.assertEqual(published['records'], 1)
            self.assertEqual(logged['event'], 'published')
            view = methods.records(source)
            self.assertEqual(view['records'][0]['close'], 10.0)
            self.assertEqual(view['raw'], packet['raw'])
            audit = bus.scan(TableQuery('collection_audit')).table.to_pylist()
            self.assertIn(logged, [json.loads(row['settings']) for row in audit])

    def test_methods_publish_accepts_non_security_packets_and_source_id_is_authoritative(self):
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {
                'news': lambda source, request: {
                    'source_id': 'spoofed',
                    'records': [{'title': '公告'}], 'raw': [{'url': 'x'}],
                    'scope': {'category': 'announcement'},
                },
            })
            source = {'id': 'announcements', 'method': 'fixture', 'params': {'key_fields': ['title']}}
            packet = methods.fetch({**source, 'method': 'news'},
                                  {'date': '2026-10-14'})
            self.assertEqual(packet['source_id'], 'announcements')
            published = methods.publish(source, packet)
            self.assertEqual(published['records'], 1)
            self.assertTrue(methods.records(source)['snapshot_id'])

    def test_collect_manual_run_uses_registered_source_and_one_methods_path(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({'id': 'fixture', 'name': 'Fixture', 'method': 'fixture',
                              'frequency': 'daily', 'defaults': {},
                              'params': {'key_fields': ['code', 'date']}})
            methods = Methods(bus, {
                'fixture': lambda source, request: {
                    'records': [{'code': '600001', 'date': request['date'], 'close': 10.0}],
                    'raw': [], 'data_as_of': request['date'],
                    'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
                },
            })
            collect = Collect(sources, methods)
            result = collect.run({'source_id': 'fixture', 'date': '2026-10-14'})
            self.assertEqual(result['outcome'], 'succeeded')
            self.assertEqual(result['records'], 1)
            self.assertEqual(collect.status(result['job_id'])['outcome'], 'succeeded')
            collect.close()

    def test_collect_freezes_source_defaults_and_request_overrides_them(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {"codes": ["old"]}})
            seen = []
            methods = Methods(bus, {
                "fixture": lambda source, request: (
                    seen.append((source["defaults"]["codes"][:], request["codes"][:])) or
                    {"records": [], "raw": [], "scope": {"category": "generic"}}),
            })
            collect = Collect(sources, methods)
            collect.run({"source_id": "fixture", "codes": ["new"]})
            self.assertEqual(seen, [(["old"], ["new"])])

    def test_default_http_transport_decodes_json_for_quote_and_announcement_methods(self):
        from collect.methods import CninfoAnnouncements, EastmoneyQuotes

        class Response:
            def __init__(self, value):
                self.value = json.dumps(value).encode()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.value

        quote = {"data": {"diff": [{
            "f2": 10.0, "f3": 1.0, "f5": 2.0, "f6": 20.0,
            "f12": "600000", "f13": 1, "f14": "Fixture", "f124": 1700000000,
        }]}}
        announcement = {"totalRecordNum": 0, "hasMore": False, "announcements": []}
        with patch("collect.methods.urlopen",
                   side_effect=[Response(quote), Response(announcement)]):
            result = EastmoneyQuotes({}).collect({"codes": ["1.600000"]})
            self.assertEqual(result["records"][0]["code"], "1.600000")
            result = CninfoAnnouncements({}).collect({
                "start": "2026-01-01", "end": "2026-01-01"})
            self.assertEqual(result["records"], [])

    def test_daily_schema_error_preserves_existing_bars_and_calendar(self):
        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            source = {'id': 'daily', 'data_type': 'daily', 'dataset_id': 'fixture'}
            methods = Methods(bus)
            calendar = _calendar(['2025-01-02', '2025-01-03'])
            bus.save_calendar('fixture', calendar)
            packet = {'records': [_daily_row('2025-01-02')], 'calendar': calendar,
                      'raw': [], 'scope': {}}
            methods.publish(source, packet)
            reference = bus.source('daily', 'bars')
            before = bus.table(reference)
            corrupt = {**_daily_row('2025-01-03'), 'name': {'bad': 'schema'}}
            with self.assertRaises((TypeError, ValueError, DataError)):
                methods.publish(source, {**packet, 'records': [corrupt],
                    'calendar': _calendar(['2025-01-04', '2025-01-05'])})
            self.assertTrue(bus.table(reference).equals(before))
            self.assertEqual(bus.calendar('fixture'), calendar)

    def test_daily_publish_is_queryable_raw_across_years_and_idempotent(self):
        from datetime import date
        from protocol import DataQuery

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            methods = Methods(bus)
            source = {'id': 'daily', 'data_type': 'daily', 'dataset_id': 'fixture'}
            dates = ['2024-12-31', '2025-01-02', '2025-01-03', '2025-01-06']
            packet = {'records': [_daily_row(dates[0], close=10.5),
                                  _daily_row(dates[1], close=11.5)],
                      'calendar': _calendar(dates[:3]), 'raw': [], 'scope': {}}
            bus.save_calendar('fixture', packet['calendar'])
            methods.publish(source, packet)
            reference = bus.source('daily', 'bars')
            columns = bus.definition(reference[5:]).columns
            bus.save_manifest('fixture', {'schema': 3, 'fields': {}, 'calendar': 'calendar:fixture',
                'scope': {'universe': 'market', 'symbols': ['600000']},
                'inputs': {'bars': reference}})
            update = {**packet, 'calendar': _calendar(dates[1:]),
                      'records': [_daily_row(dates[2], close=12.5)]}
            methods.publish(source, update)
            before = bus.table(reference)
            methods.publish(source, update)
            self.assertEqual(bus.table(reference), before)
            self.assertEqual(bus.definition(reference[5:]).columns, columns)
            self.assertEqual(bus.calendar('fixture'), _calendar(dates))
            dataset = bus.query(DataQuery('fixture', date.fromisoformat(dates[0]),
                date.fromisoformat(dates[2]), ('open', 'high', 'low', 'close', 'volume', 'amount'),
                1, 'raw', symbols=('600000',), universe='market'))
            self.assertEqual([row.trade_date.isoformat() for row in dataset.records], dates[:-1])
            self.assertEqual(dataset.records[-1].bars['600000'].close, 12.5)
            self.assertEqual(len(dataset.records[-1].frame.history['600000']), 1)

    def test_daily_publish_exposes_snapshot_page_and_data_view(self):
        from collect.core import Collect
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            reference = _bind_rows(bus, 'market', 'bars',
                [_daily_row('2026-01-02', 'sh.600000')], ('code', 'date'))
            bus.save_calendar('market', _calendar(['2026-01-02', '2026-01-05', '2026-01-06']))
            bus.save_manifest('market', {'schema': 3, 'fields': {},
                'calendar': 'calendar:market', 'scope': {'symbols': ['sh.600000'], 'universe': 'market'},
                'inputs': {'bars': reference}})
            sources = Sources(bus)
            sources.register({'id': 'market', 'name': 'Market', 'method': 'fixture',
                'frequency': 'daily', 'data_type': 'daily', 'dataset_id': 'market',
                'calendar': 'calendar:market', 'defaults': {}})
            methods = Methods(bus, {'fixture': lambda _source, _request: {
                'records': [_daily_row('2026-01-05', 'sh.600000', 11.5)],
                'raw': [], 'scope': {'symbols': ['sh.600000']}}})
            collect = Collect(sources, methods)
            self.addCleanup(collect.close)
            job = collect.run({'source_id': 'market'})
            self.assertEqual(job['outcome'], 'succeeded', job)
            summary = collect.handle('collection.sources', {})[0]
            page = collect.handle('collection.snapshot', {'id': 'market', 'offset': 1, 'limit': 1})
            self.assertEqual(summary['records'], 2)
            self.assertEqual(summary['snapshot']['snapshot_id'], page['snapshot_id'])
            self.assertEqual(page['total'], 2)
            evidence = collect.handle('collection.evidence', {'id': 'market', 'index': 1})
            self.assertEqual(evidence['snapshot_id'], page['snapshot_id'])
            self.assertEqual(evidence['record']['close'], 11.5)
            self.assertEqual(bus.source('market', 'bars'), reference)
            views = Views(bus, sources)
            self.assertEqual(views.handle('data.status', {})['总览']['日线行情']['股票数'], 1)
            self.assertEqual(views.handle('data.research_inputs', {})['summary']['symbols'], 1)

    def test_source_reports_use_actual_databus_facts(self):
        from collect.sources import Sources
        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            good = _daily_row('2025-01-02', 'sh.600036', 10.5)
            bad = {**_daily_row('2025-01-03', 'sh.600036'),
                   'open': None, 'high': 8.0, 'low': 9.0, 'close': -1.0, 'tradestatus': 0.0}
            index = {**_daily_row('2025-01-02', 'sh.000001'), 'volume': 0.0, 'amount': 0.0}
            reference = _bind_rows(bus, 'daily', 'bars', [good, bad, index], ('code', 'date'))
            _bind_rows(bus, 'duplicate', 'bars', [good], ('code', 'date'))
            _bind_rows(bus, 'stocks', 'records', [
                {'code': 'sh.600036', 'status': 'listed'},
                {'code': 'sh.000001', 'status': 'delisted'},
            ], ('code',))
            _bind_rows(bus, 'values', 'records', [{'code': 'sh.600036', 'quarter': '2025Q1', 'pe_ttm': 8.0}],
                       ('code', 'quarter'))
            _bind_rows(bus, 'facts', 'records', [{'code': 'sh.600036', 'quarter': '2025Q1', 'roe': None}],
                       ('code', 'quarter'), types={'roe': 'DOUBLE'})
            changes = bus.save_config('changes', {'events': [
                {'old_code': '600036', 'new_code': '600037', 'effective_date': '2025-01-03',
                 'factor_basis_continuity_verified': False}]})
            bus.save_manifest('market', {'schema': 3, 'fields': {},
                'scope': {'symbols': ['sh.000001', 'sh.600036'], 'universe': 'market'},
                'inputs': {'bars': reference, 'code_changes': changes}})
            from strategy import canonical_hash
            bus.save_fact({'schema': 1, 'kind': 'adjust', 'code': 'sh.600036',
                'start': '2025-01-01', 'end': '2025-01-03', 'year': None,
                'source_hash': canonical_hash([]), 'records': []})
            sources = Sources(bus)
            for name, kind in (('daily', 'daily'), ('duplicate', 'daily'),
                               ('stocks', 'stock_info'), ('values', 'valuation'), ('facts', 'fundamentals')):
                sources.register({'id': name, 'name': name, 'method': 'fixture', 'data_type': kind,
                    'frequency': 'daily', 'defaults': {}, **({'default': True, 'modes': ['fix']}
                                                            if name == 'daily' else {})})
            bus.update_registry(lambda registry: {**registry, 'datasets': [{'id': 'market'}]})

            views = Views(bus, sources)
            facts = views.source_facts()
            self.assertEqual(facts['daily']['rows'], 4)
            self.assertEqual(facts['daily']['symbols'], 2)
            self.assertEqual(facts['daily']['duplicate_keys'], 1)
            self.assertEqual(facts['daily']['invalid_price_rows'], 1)
            self.assertEqual(facts['daily']['missing_fields']['open'], 1)
            self.assertEqual(facts['daily']['trade_state_issues']['trading_without_turnover'], 1)
            self.assertEqual(facts['stock_info']['rows'], 2)
            self.assertEqual(facts['valuation']['rows'], 1)
            self.assertEqual(facts['fundamentals']['rows'], 1)
            profile = views.profile_report()
            self.assertIn('pe_ttm', profile['估值因子字段'])
            self.assertIn('roe', profile['基本面因子字段'])
            self.assertNotEqual(profile['日线字段']['open']['有值率'], 100.0)
            self.assertNotEqual(profile['结论']['级别'], '✅')
            inputs = views.research_report()
            self.assertEqual(inputs['freshness']['latest_date'], '2025-01-03')
            self.assertEqual(inputs['freshness']['latest_stock_count'], 1)
            self.assertEqual(inputs['duplicate_keys'], 1)
            self.assertEqual(inputs['price_basis_issues'], [])
            self.assertEqual(len(inputs['unverified_code_changes']), 1)
            self.assertEqual(inputs['saved_factor_files'], 1)
            self.assertTrue(any(item['可修复'] and item['fix']['source_id'] == 'daily'
                                for item in views.doctor_report()['items']))

    def test_daily_identity_normalization_deduplicates_aliases_and_keeps_markets(self):
        from datetime import date
        from protocol import DataQuery, normalize_security_id

        self.assertEqual(normalize_security_id('600036'), 'sh.600036')
        self.assertNotEqual(normalize_security_id('sh.000001'), normalize_security_id('sz.000001'))

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            symbols = ('sh.000001', 'sh.600036', 'sz.000001')
            bus.save_calendar('market', _calendar(['2026-01-02', '2026-01-05']))
            Methods(bus).publish({'id': 'market', 'data_type': 'daily', 'dataset_id': 'market'}, {
                'records': [_daily_row('2026-01-02', code) for code in ('600036', *symbols)],
                'raw': [], 'scope': {}})
            reference = bus.source('market', 'bars')
            self.assertEqual(bus.table(reference).num_rows, 3)
            bus.save_manifest('market', {'schema': 3, 'fields': {}, 'calendar': 'calendar:market',
                'inputs': {'bars': reference}, 'scope': {
                    'symbols': ['600036', 'sh.000001', 'sz.000001'], 'universe': 'market'}})
            dataset = bus.query(DataQuery('market', date(2026, 1, 2), date(2026, 1, 2),
                ('open', 'high', 'low', 'close', 'volume', 'amount'), 1, 'raw',
                symbols=symbols, universe='market'))
            self.assertEqual(set(dataset.records[0].bars), set(symbols))

    def test_cancel_stops_before_publish_and_close_stops_scheduler(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            entered, release = threading.Event(), threading.Event()
            published = []
            def handler(source, request):
                entered.set()
                release.wait(2)
                return {"records": [], "raw": [], "scope": {"category": "generic"}}
            methods = Methods(bus, {"fixture": handler})
            original = methods.publish
            methods.publish = lambda source, packet: published.append(original(source, packet))
            collect = Collect(sources, methods)
            result = []
            thread = threading.Thread(target=lambda: result.append(
                collect.run({"source_id": "fixture"})))
            thread.start()
            self.assertTrue(entered.wait(1))
            job_id = next(iter(collect._jobs))
            collect.cancel(job_id)
            release.set()
            thread.join(2)
            self.assertEqual(result[0]["outcome"], "cancelled")
            self.assertEqual(published, [])
            collect.start()
            collect.close()

    def test_collect_releases_active_when_initial_save_fails(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [], "raw": [], "scope": {"kind": "fixture"}}})
            collect = Collect(sources, methods)
            collect._save = lambda _job: (_ for _ in ()).throw(OSError("save failed"))
            result = collect.run({"source_id": "fixture"})
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(collect._active, set())

    def test_collect_log_failure_does_not_change_successful_publish(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {},
                              "params": {"key_fields": ["value"]}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"value": 1}], "raw": [], "scope": {"kind": "fixture"}}})
            methods.log = lambda _event: (_ for _ in ()).throw(OSError("log failed"))
            result = Collect(sources, methods).run({"source_id": "fixture"})
            self.assertEqual(result["outcome"], "succeeded")
            self.assertIn("log_error", result)

    def test_enabled_schedule_runs_through_collect_run(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {},
                              "params": {"key_fields": ["identity"]}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [], "raw": [], "scope": {"kind": "fixture"}}})
            collect = Collect(sources, methods)
            schedule = collect.schedule("fixture", {"enabled": True,
                                                     "interval_seconds": 60, "input": {}})
            collect.tick(at=1)
            jobs = [value for value in collect._jobs.values() if value["source_id"] == "fixture"]
            self.assertEqual(len(jobs), 0)
            self.assertEqual(collect.handle('collection.sources', {})[0]['schedule'], schedule)
            collect.tick(at=schedule["next_at"])
            jobs = [value for value in collect._jobs.values() if value["source_id"] == "fixture"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["outcome"], "succeeded")

    def test_minute_quotes_use_time_keys_and_minute_fields(self):
        from collect.methods import collect_daily
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

    def test_quote_values_must_be_finite_numbers(self):
        from collect.methods import EastmoneyQuotes, CollectionError
        import copy
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'test/data/quotes.json').read_bytes())
        for field, value in (('f2', '-'), ('f3', float('nan')), ('f5', -1), ('f6', True), ('f124', False)):
            response = copy.deepcopy(fixture['response'])
            response['data']['diff'][0][field] = value
            with self.subTest(field=field), self.assertRaises(CollectionError):
                EastmoneyQuotes({}, lambda *a: response).collect(fixture['request'])

    def test_security_lists_return_evidence_without_own_files(self):
        from collect.methods import Securities
        from types import SimpleNamespace
        import pandas as pd
        provider = SimpleNamespace(
            stock_info_sh_delist=lambda **_: pd.DataFrame([{'公司代码': '600001', '公司简称': '甲', '上市日期': '2000-01-01', '暂停上市日期': '2026-10-01'}]),
            stock_info_sz_delist=lambda **_: pd.DataFrame([{'证券代码': '000001', '证券简称': '乙', '上市日期': '2001-01-01', '终止上市日期': '2026-10-02'}]))
        result = Securities({}, provider).collect({'start': '2026-10-01', 'end': '2026-10-01', 'codes': []})
        self.assertEqual([row['bs_code'] for row in result['records']], ['sh.600001'])
        self.assertEqual(len(result['raw']), 2)

    def test_security_list_matches_bare_public_code_by_canonical_identity(self):
        from collect.methods import Securities
        import pandas as pd
        provider = SimpleNamespace(
            stock_info_sh_delist=lambda **_: pd.DataFrame([{
                '公司代码': '600036', '公司简称': '招商银行',
                '上市日期': '2000-01-01', '暂停上市日期': '2026-10-01'}]),
            stock_info_sz_delist=lambda **_: pd.DataFrame(columns=[
                '证券代码', '证券简称', '上市日期', '终止上市日期']))
        result = Securities({}, provider).collect({
            'start': '2026-10-01', 'end': '2026-10-01', 'codes': ['600036']})
        self.assertEqual([row['bs_code'] for row in result['records']], ['sh.600036'])

    def test_news_uses_current_records_and_skips_existing_dates(self):
        from collect.methods import News, Methods
        from collect.core import Collect
        from collect.sources import Sources
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><article class="content-section"><h2>标题</h2><div class="content-body"><p>正文<a href="/">链接</a>结尾</p></div></article>'
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                              'frequency': 'daily', 'defaults': {}})
            calls = []
            methods = Methods(bus, {'news': News({}, lambda url: calls.append(url) or page)})
            collect = Collect(sources, methods)
            request = {'source_id': 'news', 'start': '2026-10-01', 'end': '2026-10-01', 'codes': []}
            job = collect.run(request)
            self.assertEqual(job['outcome'], 'succeeded', job['error'])
            snapshot = methods.records(sources.get('news'))
            self.assertEqual(snapshot['records'][0]['articles'][0]['content'], '正文链接结尾')
            self.assertEqual(snapshot['raw'][0]['response'], page)
            self.assertEqual(collect.run(request)['outcome'], 'succeeded')
            self.assertEqual(len(calls), 1)
            self.assertEqual(methods.records(sources.get('news'))['total'], 1)
            self.assertEqual(list(Path(root).rglob('*.db')), [])

    def test_default_news_http_and_invalid_pages(self):
        from collect.methods import News, DEFAULT_METHODS, CollectionError
        self.assertIs(DEFAULT_METHODS.get('news'), News)
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><nav><a href="#section-1">标题</a></nav><article class="content-section" id="section-1"><h2 id="1">标题</h2><div class="content-body"><p>正文</p></div></article>'
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = page.encode()
        with patch('collect.methods.urlopen', return_value=response):
            packet = News({}).collect({'start': '2026-10-01', 'end': '2026-10-01', 'interval': 0})
        self.assertEqual(packet['records'][0]['url'], 'https://cn.govopendata.com/xinwenlianbo/20261001/')
        for bad in (page.replace('10月1日', '10月2日'), page.replace('<p>正文</p>', '<p></p>'),
                    page.replace('href="#section-1"', 'href="#section-2"'),
                    page.replace('<nav>', '<div class="toc">').replace('</nav>', '<a href="#missing">缺失</a></div>'),
                    page.replace('</nav>', '<a href="#section-1">重复</a></nav>')):
            with self.subTest(page=bad), self.assertRaises(CollectionError):
                News({}, lambda url: bad).collect({'start': '2026-10-01', 'end': '2026-10-01'})
        from urllib.error import HTTPError
        def missing(url):
            raise HTTPError(url, 404, 'not published', {}, None)
        with self.assertRaises(CollectionError):
            News({}, missing).collect({'start': '2026-10-01', 'end': '2026-10-01'})

    def test_news_schedule_uses_manual_engine_and_is_disabled_by_default(self):
        from collect.methods import News
        from collect.core import Collect
        from collect.sources import Sources
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><article class="content-section"><h2>标题</h2><div class="content-body"><p>正文</p></div></article>'
        calls = []
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); sources = Sources(bus)
            source = sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                                       'frequency': 'daily', 'defaults': {}})
            methods = Methods(bus, {'news': News({}, lambda url: calls.append(url) or page)})
            collect = Collect(sources, methods)
            settings = {'input': {'start': '2026-10-01', 'end': '2026-10-01'}, 'interval_seconds': 60}
            self.assertFalse(collect.schedule('news', settings)['enabled'])
            collect.tick(time.time() + 61)
            self.assertEqual(calls, [])
            schedule = collect.schedule('news', {**settings, 'enabled': True})
            collect.tick(schedule['next_at'])
            self.assertEqual(methods.records(source)['total'], 1)
            collect.tick(schedule['next_at'] + 61)
            self.assertEqual(len(calls), 1)
            collect.close()

    def test_news_missing_dates_and_conflicts_keep_full_content(self):
        from collect.methods import News, CollectionError
        from collect.sources import Sources
        from collect.core import Collect
        calls = []
        def transport(url):
            calls.append(url)
            number = int(url.rstrip('/').split('/')[-1][-2:])
            return f'<h1 class="article-title">2026年10月{number}日 新闻联播</h1><article class="content-section"><h2>新闻</h2><div class="content-body"><p>全文</p></div></article>'
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); sources = Sources(bus)
            source = sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                                       'frequency': 'daily', 'defaults': {}})
            methods = Methods(bus, {'news': News({}, transport)})
            with self.assertRaises(ValueError):
                methods.publish(source, {'records': [{'url': 'broken', 'date': '2026-10-01', 'articles': []}],
                                         'raw': [], 'scope': {}})
            collect = Collect(sources, methods)
            for day in ('01', '03'):
                self.assertEqual(collect.run({'source_id': 'news', 'start': f'2026-10-{day}',
                                             'end': f'2026-10-{day}'})['outcome'], 'succeeded')
            self.assertEqual(collect.run({'source_id': 'news', 'start': '2026-10-01',
                'end': '2026-10-03', 'keyword': '不匹配', 'fields': ['date']})['outcome'], 'succeeded')
            self.assertEqual([url[-3:-1] for url in calls], ['01', '03', '02'])
            self.assertEqual(methods.records(source)['total'], 3)
            self.assertEqual(methods.records(source)['records'][0]['articles'][0]['content'], '全文')
            reference = bus.source('news')
            before = bus.table(reference)
            row = methods.records(source)['records'][0]
            row['articles'][0]['content'] = '篡改'
            with self.assertRaises(DataError):
                methods.publish(source, {'records': [row], 'raw': [], 'scope': {}})
            self.assertTrue(bus.table(reference).equals(before))
            count = len(calls)
            for params in ({'timeout': 0}, {'interval': -1}, {'start': '2026-10-04', 'end': '2026-10-01'}):
                with self.assertRaises(ValueError):
                    News({}, transport).collect({'start': '2026-10-01', 'end': '2026-10-01', **params})
            self.assertEqual(len(calls), count)

    def test_latest_quote_fields_are_selectable(self):
        from collect.methods import EastmoneyQuotes
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'test/data/quotes.json').read_bytes())
        result = EastmoneyQuotes({}, lambda *a: fixture['response']).collect({**fixture['request'], 'fields': ['price', 'volume']})
        self.assertEqual(set(result['records'][0]), {'code', 'data_at', 'raw_page', 'price', 'volume'})

    def test_latest_quote_rejects_unsupported_parameters(self):
        from collect.methods import EastmoneyQuotes, CollectionError
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'test/data/quotes.json').read_bytes())
        from unittest.mock import Mock
        transport = Mock(return_value=fixture['response'])
        provider = EastmoneyQuotes({}, transport)
        for extra in ({'fields': []}, {'fields': ['unknown']}, {'frequency': 'w'}, {'adjustment': '2'}):
            with self.subTest(extra=extra), self.assertRaises(CollectionError):
                provider.collect({**fixture['request'], **extra})
        transport.assert_not_called()

    def test_daily_parameters_reach_the_provider(self):
        from collect.methods import collect_daily
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

    def test_daily_provider_maps_public_security_ids_and_adds_sdk_identity_fields(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        class Rows:
            error_code = '0'; error_msg = ''
            fields = ['date', 'code', 'close']
            def next(self): return False
        class TradeDates:
            error_code = '0'; error_msg = ''
            fields = ['calendar_date', 'is_trading_day']
            def __init__(self, start, end):
                from datetime import date, timedelta
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                self.rows = [[(first + timedelta(days=offset)).isoformat(), '1']
                             for offset in range((last - first).days + 1)]
            def next(self): return bool(self.rows)
            def get_row_data(self): return self.rows.pop(0)
        provider = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
            query_trade_dates=lambda *args, **kwargs: TradeDates(kwargs['start_date'], kwargs['end_date']),
            query_history_k_data_plus=lambda *args, **kwargs: calls.append((args, kwargs)) or Rows())
        with patch.dict('sys.modules', {'baostock': provider}):
            collect_daily({'codes': ['600036', 'sh.000001'],
                           'start': '2026-09-01', 'end': '2026-09-30',
                           'fields': ['close']})
        self.assertEqual([call[0][0] for call in calls], ['sh.600036', 'sh.000001'])
        self.assertEqual(calls[0][0][1], 'date,code,close')

    def test_daily_invalid_fields_are_rejected_before_provider_login(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        provider = SimpleNamespace(
            login=lambda: calls.append("login") or SimpleNamespace(error_code='0'),
            logout=lambda: calls.append("logout"),
        )
        with patch.dict('sys.modules', {'baostock': provider}):
            with self.assertRaises(ValueError):
                collect_daily({'codes': ['sh.600000'], 'start': '2026-09-01',
                               'end': '2026-09-30', 'fields': ['date', 'unknown']})
        self.assertEqual(calls, [])

    def test_daily_calendar_end_is_rejected_before_provider_login(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        provider = SimpleNamespace(
            login=lambda: calls.append("login") or SimpleNamespace(error_code='0'),
            logout=lambda: calls.append("logout"),
        )
        with patch.dict('sys.modules', {'baostock': provider}):
            for calendar_end in ('not-a-date', '2026-08-31'):
                with self.subTest(calendar_end=calendar_end), self.assertRaises(ValueError):
                    collect_daily({'codes': ['sh.600000'], 'start': '2026-09-01',
                                   'end': '2026-09-30', 'calendar_end': calendar_end,
                                   'fields': ['close']})
        self.assertEqual(calls, [])

    def test_trade_calendar_reports_provider_error_before_field_validation(self):
        from collect.methods import BaostockDaily, ProviderError
        class Response:
            error_code = '1001'
            error_msg = 'calendar unavailable'
            fields = []
        with self.assertRaisesRegex(ProviderError, 'calendar unavailable'):
            BaostockDaily.trade_calendar(
                SimpleNamespace(query_trade_dates=lambda **kwargs: Response()),
                '2026-10-13', '2026-10-14')

    def test_daily_can_collect_status_without_price_fields(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        class Rows:
            error_code = '0'; fields = ['date', 'code', 'isST', 'volume', 'tradestatus']
            def next(self): return False
        class TradeDates:
            error_code = '0'; error_msg = ''
            fields = ['calendar_date', 'is_trading_day']
            def __init__(self, start, end):
                from datetime import date, timedelta
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                self.rows = [[(first + timedelta(days=offset)).isoformat(), '1']
                             for offset in range((last - first).days + 1)]
            def next(self): return bool(self.rows)
            def get_row_data(self): return self.rows.pop(0)
        provider = SimpleNamespace(login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
                                   query_trade_dates=lambda *args, **kwargs: TradeDates(kwargs['start_date'], kwargs['end_date']),
                                   query_history_k_data_plus=lambda *args, **kwargs: Rows())
        with patch.dict('sys.modules', {'baostock': provider}):
            result = collect_daily({'codes': ['sz.301001'], 'start': '2026-10-01', 'end': '2026-10-02', 'fields': Rows.fields})
        self.assertEqual(result['records'], [])

    def test_eastmoney_fundamentals_fetches_one_fixed_numeric_snapshot(self):
        from collect.methods import EastmoneyFundamentals

        columns = {
            'ROEJQ': 10.08, 'ZCFZL': 91.4, 'TOTALOPERATEREVE': 1000,
            'PARENTNETPROFIT': -5, 'EPSJB': 1.2, 'BPS': 9,
            'XSMLL': None, 'XSJLL': 4, 'TOTALOPERATEREVETZ': -2,
            'PARENTNETPROFITTZ': None, 'MGJYXJJE': -0.3,
        }
        def row(code, report, notice, update, values=None):
            values = values or columns
            return {'SECUCODE': code, 'SECURITY_CODE': code[:6],
                    'REPORT_DATE': report + ' 00:00:00',
                    'NOTICE_DATE': notice + ' 00:00:00',
                    'UPDATE_DATE': update + ' 00:00:00', 'CURRENCY': 'CNY', **values}
        pages = [{'success': True, 'code': 0, 'result': {'pages': 1, 'count': 2,
                  'data': [row('000001.SZ', '2025-12-31', '2026-03-03', '2026-03-04'),
                           row('600000.SH', '2025-12-31', '2026-03-09', '2026-03-10')]}}]
        calls = []
        class Response:
            status_code = 200
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): pass
            def json(self): return self.payload
        def transport(url, *, params, timeout):
            calls.append((url, params, timeout))
            return Response(pages.pop(0))

        packet = EastmoneyFundamentals({}, transport=transport).collect({
            'codes': ['sz.000001', 'sh.600000'],
            'start': '2026-03-01', 'end': '2026-03-31',
            'fields': ['roe', 'revenue'],
        })
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['pageSize'], 500)
        self.assertEqual(calls[0][1]['filter'],
                         '(SECUCODE in ("000001.SZ","600000.SH"))'
                         "(UPDATE_DATE>='2026-03-01')(UPDATE_DATE<='2026-03-31')(CURRENCY=\"CNY\")")
        self.assertEqual([item['code'] for item in packet['records']],
                         ['sz.000001', 'sh.600000'])
        first = packet['records'][0]
        self.assertEqual(first['report_date'], '2025-12-31')
        self.assertEqual(first['notice_date'], '2026-03-03')
        self.assertEqual(first['update_date'], '2026-03-04')
        self.assertEqual(first['date'], '2026-03-04')
        self.assertEqual(first['precision'], 'date')
        self.assertEqual(first['available_at'], '2026-03-05T00:00:00+08:00')
        self.assertEqual(first['roe'], 0.1008)
        self.assertEqual(first['debt_ratio'], 0.914)
        self.assertEqual(first['net_profit'], -5)
        self.assertIsNone(first['gross_margin'])
        self.assertIn('no annualization or TTM', packet['scope']['flow_basis'])
        self.assertEqual(set(first) - {'raw_page'}, {
            'code', 'date', 'precision', 'report_date', 'notice_date', 'update_date', 'available_at',
            'roe', 'debt_ratio', 'revenue', 'net_profit', 'eps', 'book_value',
            'gross_margin', 'net_margin', 'revenue_growth', 'profit_growth', 'cash_per_share',
        })

    def test_eastmoney_fundamentals_rejects_unverifiable_rows_and_paging(self):
        from collect.methods import EastmoneyFundamentals

        values = {'ROEJQ': 10, 'ZCFZL': 50, 'TOTALOPERATEREVE': 100,
                  'PARENTNETPROFIT': -1, 'EPSJB': 1, 'BPS': 2,
                  'XSMLL': None, 'XSJLL': 5, 'TOTALOPERATEREVETZ': 2,
                  'PARENTNETPROFITTZ': 3, 'MGJYXJJE': 4}
        base = {'SECUCODE': '000001.SZ', 'SECURITY_CODE': '000001', 'CURRENCY': 'CNY',
                'REPORT_DATE': '2025-12-31 00:00:00',
                'NOTICE_DATE': '2026-03-02 00:00:00',
                'UPDATE_DATE': '2026-03-03 00:00:00', **values}

        def collect(row, *, pages=1, count=1, result_pages=None):
            payload = {'success': True, 'code': 0, 'result': {
                'pages': pages if result_pages is None else result_pages,
                'count': count, 'data': [row]}}
            class Response:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return payload
            return EastmoneyFundamentals({}, transport=lambda *a, **k: Response()).collect({
                'codes': ['sz.000001'], 'start': '2026-03-01', 'end': '2026-03-31'})

        for changed in (
            {**base, 'SECUCODE': '600000.SH'},
            {**base, 'NOTICE_DATE': None},
            {**base, 'UPDATE_DATE': '2026-03-01 00:00:00'},
            {**base, 'ROEJQ': float('inf')},
            {**base, 'SECURITY_CODE': '600000'},
        ):
            with self.subTest(changed=changed), self.assertRaises(Exception):
                collect(changed)
        with self.assertRaises(Exception):
            collect(base, count=2)
        with self.assertRaises(Exception):
            collect(base, pages=2)
        with self.assertRaises(Exception):
            collect({**base, 'CURRENCY': 'USD'})
        with self.assertRaises(Exception):
            collect(base, count=2, result_pages=1)

    def test_facts_publication_is_immutable_and_keeps_versions(self):
        from collect.methods import Methods
        from datetime import date, timedelta

        def record(update, revenue=100, gross=None):
            return {
                'code': 'sz.000001', 'date': update, 'report_date': '2025-12-31',
                'precision': 'date',
                'notice_date': '2026-03-02', 'update_date': update,
                'available_at': (date.fromisoformat(max(update, '2026-03-02'))
                                 + timedelta(days=1)).isoformat() + 'T00:00:00+08:00',
                'roe': 0.1, 'debt_ratio': 0.5, 'revenue': revenue, 'net_profit': -1,
                'eps': 1.0, 'book_value': 2.0, 'gross_margin': gross, 'net_margin': 0.05,
                'revenue_growth': 0.02, 'profit_growth': None, 'cash_per_share': -0.3,
            }

        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            methods = Methods(bus)
            source = {'id': 'fundamentals', 'method': 'eastmoney_fundamentals',
                      'data_type': 'facts', 'storage_id': 'fundamentals', 'dataset_id': 'cn_a'}
            packet = {'records': [record('2026-03-03')], 'raw': [{'page': 1}],
                      'scope': {'codes': ['sz.000001']}, 'request': {'refresh': True}}

            self.assertEqual(methods.prepare(source, packet)['incoming'], 1)
            with self.assertRaises(DataError) as caught:
                bus.source('fundamentals')
            self.assertEqual(caught.exception.code, 'missing')

            self.assertEqual(methods.publish(source, packet)['records'], 1)
            reference = bus.source('fundamentals')
            definition = bus.definition(reference[5:])
            self.assertEqual(definition.keys, ('code', 'report_date', 'available_at'))
            self.assertEqual(definition.columns['revenue'], 'DOUBLE')
            before = bus.table(reference)
            self.assertEqual(before.num_rows, 1)
            self.assertIsNone(before.column('gross_margin')[0].as_py())
            for changed in (record('2026-03-03', gross=0.2),
                            record('2026-03-03', revenue=200)):
                result = methods.publish(source, {**packet, 'records': [changed]})
                self.assertEqual(result['records'], 0)
                self.assertEqual([item['code'] for item in result['failures']], ['sz.000001'])
            self.assertTrue(bus.table(reference).equals(before))

            next_version = record('2026-03-10', revenue=120, gross=0.25)
            methods.publish(source, {**packet, 'records': [next_version]})
            history = bus.table(reference, order=('available_at',)).to_pylist()
            self.assertEqual([row['revenue'] for row in history], [100, 120])

    def test_eastmoney_fundamentals_accepts_only_the_observed_empty_window_code(self):
        from collect.methods import EastmoneyFundamentals, Methods

        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {'version': None, 'result': None, 'success': False,
                        'message': '返回数据为空', 'code': 9201}

        source = {'id': 'fundamentals', 'method': 'eastmoney_fundamentals',
                  'data_type': 'facts', 'storage_id': 'fundamentals', 'dataset_id': 'cn_a',
                  'params': {'key_fields': ['code', 'report_date', 'available_at']}}
        packet = EastmoneyFundamentals({}, transport=lambda *args, **kwargs: Response()).collect({
            'codes': ['sz.000001'], 'start': '2026-03-01', 'end': '2026-03-02'})
        self.assertEqual(packet['records'], [])
        self.assertEqual(packet['scope']['observed_records'], 0)
        self.assertEqual(len(packet['raw']), 1)
        with tempfile.TemporaryDirectory() as root, fixture_data(Path(root)) as bus:
            result = Methods(bus).publish(source, packet)
            self.assertEqual(result['records'], 0)
            with self.assertRaises(DataError) as caught:
                bus.source('fundamentals')
            self.assertEqual(caught.exception.code, 'missing')
            events = bus.scan(TableQuery('collection_audit')).table.to_pylist()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]['settings'])['raw'], packet['raw'])

    def test_financial_versions_reach_databus_without_rewriting_history(self):
        from collect.methods import EastmoneyFundamentals
        from core.calculation import Calculation
        from tests.history_fixture import make_snapshot
        from protocol import DataQuery
        from tests.test_history_data import FIELDS
        from datetime import date, timedelta

        def record(code, update, roe):
            return {**dict.fromkeys(EastmoneyFundamentals.value_columns, None),
                    'code': code, 'date': update, 'precision': 'date',
                    'report_date': '2019-09-30', 'notice_date': '2020-01-30',
                    'update_date': update,
                    'available_at': (date.fromisoformat(update) + timedelta(days=1)).isoformat() + 'T00:00:00+08:00',
                    'roe': roe}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root, symbols=('600003', '600004'))
            bus = DataBus(DataStore(root)); methods = Methods(bus)
            source = {'id': 'fundamentals', 'method': 'eastmoney_fundamentals',
                      'data_type': 'facts', 'storage_id': 'fundamentals', 'dataset_id': 'fixture'}
            args = dict(symbols=('600003', '600004'), history=3,
                        start=date(2020, 1, 30), end=date(2020, 2, 4))
            def history(fields=FIELDS):
                return bus.query(DataQuery('fixture', args['start'], args['end'],
                    fields, args['history'], 'adjusted', symbols=args['symbols'],
                    market={'scope': 'CN_A', 'codes': list(args['symbols'])}, tax='none'))

            price_before = history()
            packet = {'records': [record('sh.600003', '2020-01-30', .1),
                                  record('sh.600004', '2020-01-30', .3)],
                      'raw': [], 'scope': {}}
            methods.publish(source, packet)
            reference = bus.source('fundamentals')
            table = bus.table(reference, order=('code', 'available_at'))
            methods.publish(source, packet)
            self.assertTrue(bus.table(reference, order=('code', 'available_at')).equals(table))
            manifest = bus.manifest('fixture')
            manifest['files'].append({'file': reference, 'role': 'market'})
            manifest['fields']['roe'] = {'source': 'facts', 'file': reference, 'code': 'code',
                'effective': 'report_date', 'known': 'available_at', 'column': 'roe', 'unit': 'ratio'}
            bus.save_manifest('fixture', manifest)
            before = history((*FIELDS, 'roe'))
            methods.publish(source, {**packet, 'records': [record('sh.600003', '2020-02-03', .5)]})
            after = history((*FIELDS, 'roe'))
            frames = {row.trade_date: row.frame for row in after.records}
            self.assertIsNone(frames[date(2020, 1, 30)].fields['600003']['roe'])
            self.assertEqual(frames[date(2020, 1, 31)].fields['600003']['roe'], .1)
            self.assertEqual(frames[date(2020, 2, 3)].fields['600003']['roe'], .1)
            self.assertEqual(frames[date(2020, 2, 4)].fields['600003']['roe'], .5)
            self.assertEqual([r.frame for r in before.records if r.trade_date < date(2020, 2, 4)],
                             [r.frame for r in after.records if r.trade_date < date(2020, 2, 4)])
            last = frames[date(2020, 2, 4)]
            old = next(row for row in last.history['600003'] if row.at.date() == date(2020, 1, 31))
            self.assertEqual(old.fields['roe'], .1)
            model = Calculation.fundamentals(last, {'roe': 1.0})
            self.assertAlmostEqual(model.fields['600003']['score'], 1.)
            self.assertAlmostEqual(model.fields['600004']['score'], -1.)
            self.assertEqual([row.frame for row in price_before.records],
                             [row.frame for row in history().records])

    def test_financial_pages_fail_closed_and_keep_source_evidence(self):
        from copy import deepcopy
        from collect.methods import EastmoneyFundamentals, CollectionError
        from unittest.mock import Mock

        row = {column: 1.0 for column in EastmoneyFundamentals.columns}
        row.update(SECUCODE='600003.SH', SECURITY_CODE='600003', CURRENCY='CNY',
                   REPORT_DATE='2025-12-31', NOTICE_DATE='2026-03-01', UPDATE_DATE='2026-03-02')
        second = {**row, 'SECUCODE': '600004.SH', 'SECURITY_CODE': '600004'}
        def page(rows, count=2):
            return {'success': True, 'code': 0, 'result': {'pages': 2, 'count': count, 'data': rows}}
        def adapter(payloads):
            get = Mock(side_effect=[SimpleNamespace(raise_for_status=lambda: None,
                                    json=lambda payload=payload: payload) for payload in payloads])
            obj = EastmoneyFundamentals({}, transport=get); obj.page_size = 1
            return obj, get
        request = {'codes': ['sh.600003', 'sh.600004'], 'start': '2026-03-01', 'end': '2026-03-31'}
        good, get = adapter([page([row]), page([second])])
        packet = good.collect(request)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([item['raw_page'] for item in packet['records']], [0, 1])
        for payloads in ([page([row]), page([second], 3)], [page([row]), page([row])],
                         [page([row]), {'success': False, 'code': 9201, 'result': None}],
                         [{'success': False, 'code': 9202, 'result': None}]):
            bad, _ = adapter(payloads)
            with self.assertRaises(CollectionError) as caught:
                bad.collect(request)
            self.assertTrue(caught.exception.evidence)
        for changed in ({'codes': ['bj.830001']}, {'fields': ['unknown']},
                        {'codes': ['zz.600003']}):
            bad, get = adapter([])
            with self.assertRaises(CollectionError):
                bad.collect({**request, **changed})
            get.assert_not_called()
        with tempfile.TemporaryDirectory() as temp, fixture_data(Path(temp)) as bus:
            methods = Methods(bus)
            source = {'id': 'fundamentals', 'method': 'eastmoney_fundamentals',
                      'data_type': 'facts', 'storage_id': 'fundamentals', 'dataset_id': 'fixture'}
            self.assertEqual(methods.publish(source, packet)['records'], 2)
            audit = bus.scan(TableQuery('collection_audit')).table.to_pylist()
            self.assertEqual(json.loads(audit[0]['settings'])['raw'], packet['raw'])

    def test_financial_latest_starts_at_the_oldest_security_watermark(self):
        from collect.methods import Methods
        source = {'id': 'fundamentals', 'method': 'eastmoney_fundamentals', 'data_type': 'facts'}
        with tempfile.TemporaryDirectory() as temp, fixture_data(Path(temp)) as bus:
            methods = Methods(bus)
            _bind_rows(bus, 'fundamentals', 'records', [
                {'code': 'sh.600003', 'date': '2026-03-21'},
                {'code': 'sh.600004', 'date': '2026-03-31'},
            ], ('code', 'date'))
            self.assertEqual(methods.last_date(source, ['sh.600003', 'sh.600004']), '2026-03-21')
            self.assertIsNone(methods.last_date(source, ['sh.600003', 'sh.600005']))

    def test_facts_publication_validates_schema_rolls_back_and_checks_existing_rows(self):
        handler = SimpleNamespace(
            text_columns=('code', 'date', 'period', 'visible_at'),
            value_columns={'value': ('VALUE', 'ratio', False)},
            dates=('period', 'visible_at'), keys=('code', 'period', 'visible_at'),
            validate_record=lambda row: None)
        source = {'id': 'numeric', 'method': 'custom_numeric', 'data_type': 'facts',
                  'storage_id': 'numeric', 'dataset_id': 'fixture'}
        row = {'code': 'sh.600003', 'date': '2020-01-30',
               'period': '2019-09-30', 'visible_at': '2020-01-31T00:00:00+08:00',
               'value': .25}
        packet = {'records': [row], 'raw': [], 'scope': {}}
        with tempfile.TemporaryDirectory() as temp, fixture_data(Path(temp)) as bus:
            methods = Methods(bus, handlers={'custom_numeric': handler})
            handler.text_columns = ('code', 'date', 'period')
            with self.assertRaisesRegex(DataError, 'time columns'):
                methods.publish(source, packet)
            handler.text_columns = (*handler.text_columns, 'visible_at')
            with patch.object(bus, 'bind', side_effect=OSError('simulated binding failure')):
                with self.assertRaisesRegex(OSError, 'simulated binding failure'):
                    methods.publish(source, packet)
            with self.assertRaises(DataError) as caught:
                bus.source('numeric')
            self.assertEqual(caught.exception.code, 'missing')
            from market import table_name
            with self.assertRaises(DataError) as caught:
                bus.definition(table_name('numeric/records'))
            self.assertEqual(caught.exception.code, 'missing_table')
            methods.publish(source, packet)
            reference = bus.source('numeric')
            self.assertEqual(bus.table(reference).to_pylist(), [row])
            self.assertEqual(bus.definition(reference[5:]).keys, handler.keys)
            before = bus.table(reference)
            empty = {**packet, 'records': []}
            self.assertEqual(methods.prepare(source, empty)['incoming'], 0)
            self.assertEqual(methods.publish(source, empty)['records'], 0)
            self.assertTrue(bus.table(reference).equals(before))

            def reject(_row):
                raise ValueError('stored observation is invalid')

            handler.validate_record = reject
            for deliver in (methods.prepare, methods.publish):
                with self.assertRaisesRegex(ValueError, 'stored observation is invalid'):
                    deliver(source, empty)
            self.assertTrue(bus.table(reference).equals(before))
