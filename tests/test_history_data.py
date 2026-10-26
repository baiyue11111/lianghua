from protocol import normalize_security_id
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market import DataBus
from protocol import DataError, DataQuery
from storage import DataStore
from tests.history_fixture import make_snapshot

FIELDS = ("open", "high", "low", "close", "volume", "amount", "signal_close")


def query(root, *, fields=FIELDS, symbols=("600003",), history=20,
          start=date(2020, 1, 31), end=date(2020, 2, 4), universe="dataset"):
    return DataBus(DataStore(root)).query(
        DataQuery("fixture", start, end, fields, history, "adjusted", symbols=symbols, universe=universe)
    )


def payload(**changes):
    value = {
        "run_id": "history-test", "module": "plugins.strategies.breadth_low_amount_reversal",
        "params": {"positions": 4}, "initial_cash": "100000", "dataset_id": "fixture",
        "start": "2020-01-31", "end": "2020-02-04",
        "limits": {"stock_weight": "0.4", "adv_fraction": "0.01"},
        "execution": {
            "price": {"model": "ratio", "params": {"rate": ".001", "tick": ".01"}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": ".01", "min_qty": 100, "step": 100}},
        },
    }
    value.update(changes)
    return value


class HistoricalDataTests(unittest.TestCase):
    def test_numeric_facts_are_point_in_time_and_projected_from_declared_parquet(self):
        import hashlib
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=('600003', '600004'))
            folder = root / 'data/source/fixture'
            path = folder / 'fundamentals.parquet'
            pq.write_table(pa.Table.from_pylist([
                {'code': '600003', 'report_date': date(2019, 9, 30), 'published_at': '2020-01-30T16:00:00+08:00', 'roe': .1, 'book': 1.},
                {'code': '600004', 'report_date': date(2019, 9, 30), 'published_at': '2020-01-30T16:00:00+08:00', 'roe': .3, 'book': 3.},
                {'code': '600003', 'report_date': date(2019, 12, 31), 'published_at': '2020-01-31T16:00:00+08:00', 'roe': .2, 'book': 2.},
                {'code': '600004', 'report_date': date(2019, 12, 31), 'published_at': '2020-01-31T16:00:00+08:00', 'roe': .4, 'book': 4.},
                {'code': '600003', 'report_date': date(2019, 9, 30), 'published_at': '2020-02-04T16:00:00+08:00', 'roe': .9, 'book': 9.},
            ]), path)
            manifest = folder / 'manifest.json'
            value = json.loads(manifest.read_text())
            value['files'].append({'file': 'fundamentals.parquet', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'role': 'market'})
            binding = {'source': 'facts', 'file': 'fundamentals.parquet', 'code': 'code',
                       'effective': 'report_date', 'known': 'published_at', 'unit': 'ratio'}
            value['fields'] = {'roe': {**binding, 'column': 'roe'}, 'book': {**binding, 'column': 'book'}}
            manifest.write_text(json.dumps(value))
            fields = (*FIELDS, 'roe', 'book')
            import duckdb
            from unittest.mock import patch
            reads = []
            real_connect = duckdb.connect
            class Connection:
                def __init__(self): self.inner = real_connect()
                def execute(self, sql, params=None):
                    if 'read_parquet' in sql and 'fundamentals.parquet' in str(params): reads.append(sql)
                    return self.inner.execute(sql, params)
                def close(self): self.inner.close()
            with patch('market.reader.duckdb.connect', side_effect=Connection):
                dataset = DataBus(DataStore(root)).query(DataQuery('fixture', date(2020, 1, 31), date(2020, 2, 4),
                    fields, 3, 'adjusted', symbols=('600004', '600003')))
            by_day = {record.trade_date: record for record in dataset.records}
            self.assertIsNone(by_day[date(2020, 1, 30)].frame.fields['600003']['roe'])
            self.assertEqual(by_day[date(2020, 1, 31)].frame.fields['600003']['roe'], .1)
            self.assertEqual(by_day[date(2020, 1, 31)].frame.fields['600004']['roe'], .3)
            self.assertEqual(by_day[date(2020, 2, 3)].frame.fields['600003']['roe'], .2)
            self.assertEqual(by_day[date(2020, 2, 4)].frame.fields['600003']['roe'], .2)
            self.assertEqual(tuple(by_day[date(2020, 2, 3)].frame.fields), ('600004', '600003'))
            self.assertEqual(by_day[date(2020, 2, 3)].frame.history['600003'][-1].fields['roe'], .2)
            self.assertEqual(len(reads), 1)
            self.assertTrue(any(key.endswith('/fundamentals.parquet')
                                for key in dataset.provenance['source_hashes']))

    def test_numeric_facts_use_each_field_code_binding(self):
        import hashlib
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=('600003', '600004'))
            folder = root / 'data/source/fixture'
            path = folder / 'fundamentals.parquet'
            pq.write_table(pa.Table.from_pylist([
                {'roe_code': '600003', 'book_code': '600004', 'report_date': date(2019, 12, 31),
                 'published_at': '2020-01-30T16:00:00+08:00', 'roe': .1, 'book': 4.},
                {'roe_code': '600004', 'book_code': '600003', 'report_date': date(2019, 12, 31),
                 'published_at': '2020-01-30T16:00:00+08:00', 'roe': .3, 'book': 3.},
            ]), path)
            manifest = folder / 'manifest.json'
            value = json.loads(manifest.read_text())
            value['files'].append({'file': 'fundamentals.parquet',
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'role': 'market'})
            common = {'source': 'facts', 'file': 'fundamentals.parquet',
                      'effective': 'report_date', 'known': 'published_at', 'unit': 'ratio'}
            value['fields'] = {
                'roe': {**common, 'code': 'roe_code', 'column': 'roe'},
                'book': {**common, 'code': 'book_code', 'column': 'book'},
            }
            manifest.write_text(json.dumps(value))
            fields = (*FIELDS, 'roe', 'book')
            dataset = DataBus(DataStore(root)).query(DataQuery('fixture', date(2020, 1, 31),
                date(2020, 2, 4), fields, 3, 'adjusted', symbols=('600003', '600004')))
            by_symbol = dataset.records[-1].frame.fields
            self.assertEqual(by_symbol['600003']['roe'], .1)
            self.assertEqual(by_symbol['600004']['roe'], .3)
            self.assertEqual(by_symbol['600003']['book'], 3.)
            self.assertEqual(by_symbol['600004']['book'], 4.)

    def test_research_keeps_unobserved_codes_visible_in_data_quality(self):
        import hashlib
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root=Path(temp);make_snapshot(root,symbols=('600003','600004'))
            path=root/'data/source/fixture/daily/bars.parquet'
            rows=pq.read_table(path).to_pylist()
            pq.write_table(pa.Table.from_pylist([r for r in rows if r['code']=='600003']),path)
            manifest=root/'data/source/fixture/manifest.json';value=json.loads(manifest.read_text())
            for ref in value['files']:
                if ref['file']=='daily/bars.parquet':ref['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(value))
            request=DataQuery('fixture',date(2020,1,31),date(2020,2,4),FIELDS,3,'adjusted',
                symbols=('600003','600004'),tax='none',accounting='research')
            result=DataBus(DataStore(root)).query(request)
            self.assertEqual(result.scope['symbols'],('600003','600004'))
            self.assertEqual(result.provenance['data_quality']['missing_prices'],['600004'])
            self.assertIsNone(result.records[-1].frame.fields['600004']['close'])

    def test_research_does_not_require_distribution_settlement_files(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            make_snapshot(root,symbols=('600003',))
            for path in (root/'data/source/fixture/corp').glob('dividend_*.json'):
                path.unlink()
            manifest=root/'data/source/fixture/manifest.json'
            value=json.loads(manifest.read_text())
            value['files']=[ref for ref in value['files'] if not ref['file'].startswith('corp/dividend_')]
            manifest.write_text(json.dumps(value))
            request=DataQuery('fixture',date(2020,1,31),date(2020,2,4),FIELDS,3,'adjusted',
                symbols=('600003',),tax='none',accounting='research')
            dataset=DataBus(DataStore(root)).query(request)
            self.assertFalse(any(r.registrations for r in dataset.records))
            self.assertEqual(dataset.provenance['accounting'],'research')
            self.assertEqual(dataset.provenance['position_unit'],'adjusted_units')
            self.assertTrue(dataset.records[-1].frame.context['back_factors'])

    def test_price_query_uses_manifest_without_scanning_source_store(self):
        from unittest.mock import patch
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=('600003',))
            expected = query(root, history=3)
            with patch.object(DataStore, 'list', side_effect=AssertionError('unnecessary source scan')):
                actual = query(root, history=3)
            self.assertEqual(actual, expected)

    def test_pretax_carries_share_claim_until_sourced_listing(self):
        from strategy import canonical_hash
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/corp/dividend_sh.600003_2020.json"
            value = json.loads(path.read_text())
            value['records'] = [dict(announce_date='2020-01-20', registration_date='2020-01-30',
                operation_date='2020-01-31', payment_date='2020-02-03', stock_market_date='2020-02-04',
                cash_per_share=0, stock_dividend_per_share=.1, reserve_to_stock_per_share=0,
                cash_after_tax_text=None)]
            value['source_hash'] = canonical_hash(value['records'])
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(DataError, 'sourced credit and tax'):
                query(root, history=3)
            request = DataQuery('fixture', date(2020, 1, 31), date(2020, 2, 4),
                FIELDS, 3, 'adjusted', symbols=('600003',), tax='none')
            dataset = DataBus(DataStore(root)).query(request)
            actions = [action for record in dataset.records for action in record.registrations]
            self.assertEqual(actions[0].share_credit_date, date(2020, 2, 4))
            self.assertIsNone(actions[0].stock_nominal)
            self.assertIsNone(actions[0].tax_source_sha256)
            self.assertEqual(dataset.provenance['return_basis'], 'pretax_after_costs')
            value['records'][0]['payment_date'] = None
            value['source_hash'] = canonical_hash(value['records'])
            path.write_text(json.dumps(value))
            without_payment = DataBus(DataStore(root)).query(request)
            action = next(a for r in without_payment.records for a in r.registrations)
            self.assertEqual(action.share_credit_date, date(2020, 2, 4))
            value['records'][0]['payment_date'] = '2020-02-03'
            value['records'][0]['cash_per_share'] = .2
            value['source_hash'] = canonical_hash(value['records'])
            path.write_text(json.dumps(value))
            mixed = DataBus(DataStore(root)).query(request)
            action = next(a for r in mixed.records for a in r.registrations)
            self.assertEqual(action.share_credit_date, date(2020, 2, 4))
            self.assertEqual(action.event.payment_date, date(2020, 2, 3))
            value['records'][0]['stock_market_date'] = None
            value['source_hash'] = canonical_hash(value['records'])
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(DataError, 'settlement evidence incomplete'):
                DataBus(DataStore(root)).query(request)

    def test_index_reads_standard_records_after_each_publication(self):
        from plugins.data import Adapter
        class Source:
            records = [dict(code='sh.600003', index='I', effective='2020-01-01',
                            known='2020-01-01T08:00:00+08:00', included=True)]
            def read(self, kind, source, file):
                if source == 'registry':
                    return json.dumps({'markets': [{
                        'scope': 'EXCHANGE', 'dataset_id': 'fixture',
                        'code_pattern': r'sh\.60\d{4}',
                        'members': dict(source='components', file='records.json', code='code',
                                        index='index', effective='effective', known='known',
                                        included='included')}]}).encode()
                self.assert_source(source, file)
                return json.dumps({'records': self.records}).encode()
            def assert_source(self, source, file):
                assert (source, file) == ('components', 'records.json')
        source = Source()
        args = ('fixture', date(2020, 1, 1), date(2020, 2, 1))
        market = {'scope': 'EXCHANGE', 'index': 'I'}
        self.assertEqual(Adapter.candidates(source, market, *args), ('sh.600003',))
        source.records.append(dict(code='sh.600004', index='I', effective='2020-01-20',
                                   known='2020-01-19T08:00:00+08:00', included=True))
        self.assertEqual(Adapter.candidates(source, market, *args), ('sh.600003', 'sh.600004'))

    def test_index_rejects_records_that_do_not_cover_requested_history(self):
        from plugins.data import Adapter
        registry = dict(scope='TEST', timezone='Asia/Shanghai', session_open='09:30',
                        session_close='15:00', fields={},
                        members=dict(code='code', index='index', effective='effective',
                                     known='known', included='included'))
        rows = [dict(code='A', index='I', effective='2026-01-01',
                     known='2026-01-01T08:00:00+08:00', included=True)]
        with self.assertRaisesRegex(ValueError, 'history'):
            Adapter.eligible({'scope': 'TEST', 'index': 'I'}, registry,
                             (date(2014, 1, 2),), ('A',), {}, rows)

    def test_full_market_candidates_use_registered_code_pattern(self):
        from plugins.data import Adapter
        class Source:
            def read(self, *args):
                return json.dumps({'markets': [{
                    'scope': 'EXCHANGE', 'dataset_id': 'fixture',
                    'code_normalizer': 'normalize_security_id',
                    'code_pattern': r'(?:sh\.(?:60|68)\d{4}|sz\.(?:00|30)\d{4})',
                }]}).encode()
            def manifest(self, dataset):
                return {'scope': {'symbols': ['600003', 'sz.000001', 'sh.000300', 'bj.430047']}}
        source = Source()
        args = ('fixture', date(2020, 1, 1), date(2020, 2, 1))
        self.assertEqual(Adapter.candidates(source, {'scope': 'EXCHANGE'}, *args),
                         ('600003', 'sz.000001'))
        with self.assertRaisesRegex(ValueError, 'outside.*market'):
            Adapter.candidates(source, {'scope': 'EXCHANGE', 'codes': ['sh.000300']}, *args)
        with self.assertRaisesRegex(ValueError, 'membership'):
            Adapter.candidates(source, {'scope': 'EXCHANGE', 'index': 'I'}, *args)

    def test_full_market_eligibility_does_not_require_membership(self):
        from plugins.data import Adapter
        day = date(2020, 1, 2)
        registry = {'scope': 'EXCHANGE', 'timezone': 'Asia/Shanghai',
                    'session_open': '09:30', 'session_close': '15:00',
                    'fields': {}, 'code_pattern': r'sh\.60\d{4}'}
        closing, opening = Adapter.eligible({'scope': 'EXCHANGE'}, registry,
            (day,), ('sh.600003', 'sh.000300'), {}, ())
        self.assertEqual(closing[day], ('sh.600003',))
        self.assertEqual(opening[day], ('sh.600003',))

    def test_full_market_reads_every_matching_stock_without_membership(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=('600003', '600004'))
            path = root / 'data/source/registry/sources.json'
            registry = json.loads(path.read_text())
            self.assertNotIn('members', registry['markets'][0])
            registry['markets'][0]['code_pattern'] = r'sh\.60\d{4}'
            path.write_text(json.dumps(registry))
            bus = DataBus(DataStore(root))
            dataset = bus.query(DataQuery('fixture', date(2020, 1, 31), date(2020, 2, 4),
                               FIELDS, 20, 'adjusted', market={'scope': 'CN_A'}))
            self.assertEqual(dataset.scope['symbols'], ('600003', '600004'))
            self.assertEqual(tuple(dataset.records[-1].frame.fields), ('600003', '600004'))

    def test_market_eligibility_uses_effective_and_known_dates_and_and_filters(self):
        from plugins.data import Adapter

        days = (date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6))
        registry = {
            "scope": "TEST", "currency": "CNY", "calendar": "fixture",
            "execution": "fixture", "timezone": "Asia/Shanghai",
            "session_open": "09:30", "session_close": "15:00",
            "members": {"code": "code", "index": "index", "effective": "effective",
                        "known": "known", "included": "included"},
            "fields": {"close": {"type": "number", "source": "daily", "column": "close",
                                  "effective": "date", "known": "date"}},
        }
        membership = (
            {"code": "A", "index": "test_index", "effective": "2020-01-02",
             "known": "2020-01-02T08:00:00+08:00", "included": True},
            {"code": "A", "index": "test_index", "effective": "2020-01-03",
             "known": "2020-01-02T16:00:00+08:00", "included": False},
            {"code": "B", "index": "test_index", "effective": "2020-01-03",
             "known": "2020-01-06T08:00:00+08:00", "included": True},
        )
        daily = {
            date(2020, 1, 2): {"A": {"close": {"value": 12.0, "known_at": "2020-01-02T15:00:00+08:00"}},
                               "B": {"close": {"value": 15.0, "known_at": "2020-01-02T15:00:00+08:00"}}},
            date(2020, 1, 3): {"A": {"close": {"value": 13.0, "known_at": "2020-01-03T15:00:00+08:00"}},
                               "B": {"close": {"value": 14.0, "known_at": "2020-01-03T15:00:00+08:00"}}},
            date(2020, 1, 6): {"A": {"close": {"value": 13.0, "known_at": "2020-01-06T15:00:00+08:00"}},
                               "B": {"close": {"value": 14.0, "known_at": "2020-01-06T15:00:00+08:00"}}},
        }

        eligible, opening = Adapter.eligible(
            {"scope": "TEST", "index": "test_index", "codes": ["A", "B"],
             "filters": [{"field": "close", "op": "gt", "value": 10}]},
            registry, days, ("A", "B"), daily, membership,
        )

        self.assertEqual(eligible, {
            date(2020, 1, 2): ("A",),
            date(2020, 1, 3): (),
            date(2020, 1, 6): ("B",),
        })
        self.assertEqual(opening, {
            date(2020, 1, 2): (),
            date(2020, 1, 3): (),
            date(2020, 1, 6): ("B",),
        })

    def test_future_close_cannot_change_same_day_opening_eligibility(self):
        from plugins.data import Adapter

        day = date(2020, 1, 3)
        registry = {
            "scope": "TEST", "currency": "CNY", "calendar": "fixture",
            "execution": "fixture", "timezone": "Asia/Shanghai",
            "session_open": "09:30", "session_close": "15:00",
            "members": {"code": "code", "effective": "effective",
                "known": "known", "included": "included"},
            "fields": {"close": {"type": "number", "source": "daily", "column": "close",
                                  "effective": "date", "known": "bar_time"}},
        }
        market = {"scope": "TEST", "codes": ["A"],
                  "filters": [{"field": "close", "op": "gte", "value": 30}]}
        membership = ({"code": "A", "effective": "2020-01-01",
                       "known": "2020-01-01T08:00:00+08:00", "included": True},)
        before = {date(2020, 1, 2): {"A": {"close": {"value": 35.0,
                  "known_at": "2020-01-02T15:00:00+08:00"}}},
                  day: {"A": {"close": {"value": 1.0,
                  "known_at": "2020-01-03T15:00:00+08:00"}}}}
        after = {date(2020, 1, 2): {"A": {"close": {"value": 35.0,
                  "known_at": "2020-01-02T15:00:00+08:00"}}},
                 day: {"A": {"close": {"value": 99.0,
                  "known_at": "2020-01-03T15:00:00+08:00"}}}}
        first = Adapter.eligible(market, registry, (day,), ("A",), before, membership)
        second = Adapter.eligible(market, registry, (day,), ("A",), after, membership)
        self.assertEqual(first[1][day], second[1][day])
        self.assertEqual(first[1][day], ("A",))

    def test_market_filter_rejects_unknown_field_and_invalid_operator(self):
        from plugins.data import Adapter

        registry = {
            "scope": "TEST", "currency": "CNY", "calendar": "fixture",
            "execution": "fixture", "timezone": "Asia/Shanghai",
            "session_open": "09:30", "session_close": "15:00",
            "members": {"code": "code", "index": "index",
                "effective": "effective", "known": "known", "included": "included"},
            "fields": {"close": {"type": "number", "source": "daily", "column": "close",
                                  "effective": "date", "known": "date"}},
        }
        for condition in ({"field": "secret", "op": "eq", "value": 1},
                          {"field": "close", "op": "eval", "value": "1"}):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                Adapter.eligible({"scope": "TEST", "filters": [condition]}, registry,
                                 (date(2020, 1, 2),), ("A",),
                                 {date(2020, 1, 2): {"A": {"close": {"value": 1.0,
                                     "known_at": "2020-01-02T15:00:00+08:00"}}}}, ())

    def test_market_filter_validates_every_expected_value_without_candidates(self):
        from plugins.data import Adapter

        registry = {
            "scope": "TEST", "timezone": "Asia/Shanghai", "session_open": "09:30",
            "session_close": "15:00", "members": {
                "code": "code", "effective": "effective", "known": "known",
                "included": "included",
            }, "fields": {"close": {"type": "number", "source": "daily", "column": "close"}},
        }
        membership = ({"code": "A", "effective": "2020-01-01",
                       "known": "2020-01-01T08:00:00+08:00", "included": False},)
        for condition in (
            {"field": "close", "op": "between", "value": [1, "bad"]},
            {"field": "close", "op": "in", "value": [1, "bad"]},
            {"field": "close", "op": "between", "value": [2, 1]},
        ):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                Adapter.eligible({"scope": "TEST", "filters": [condition]}, registry,
                                 (date(2020, 1, 2),), ("A",), {}, membership)

    def test_empty_market_pool_is_a_valid_backtest_no_decision(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort

        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            result = KernelBacktestPort(DataBus(DataStore(root)), Settlement).handle(
                "backtest.run", payload(
                    run_id="empty-market-pool",
                    module="plugins.strategies.moving_average_cross",
                    params={"fast_window": 2, "slow_window": 3},
                    market={"scope": "CN_A", "codes": ["600003"],
                            "filters": [{"field": "close", "op": "gt",
                                         "value": 1_000_000_000}]},
                )).data

            self.assertTrue(result["equity"])
            self.assertEqual(result["trades"], [])

    def test_market_scope_and_index_intersect_explicit_codes(self):
        from plugins.data import Adapter

        day = date(2020, 1, 2)
        registry = {
            "scope": "TEST", "timezone": "Asia/Shanghai", "session_open": "09:30",
            "session_close": "15:00", "fields": {},
            "members": {"code": "code", "index": "index", "effective": "effective",
                        "known": "known", "included": "included"},
        }
        rows = (
            {"code": "A", "index": None, "effective": "2020-01-01",
             "known": "2020-01-01T08:00:00+08:00", "included": True},
            {"code": "B", "index": "I", "effective": "2020-01-01",
             "known": "2020-01-01T08:00:00+08:00", "included": True},
        )
        result, _ = Adapter.eligible({"scope": "TEST", "codes": ["A", "B"], "index": "I"},
                                     registry, (day,), ("A", "B"), {}, rows)
        self.assertEqual(result[day], ("B",))

    def test_market_date_does_not_truncate_invalid_suffix_and_decimal_is_numeric(self):
        from plugins.data import _effective, _market_value

        with self.assertRaises(ValueError):
            _effective("2020-01-02garbage")
        self.assertEqual(_market_value(Decimal("30.5"), "number"), Decimal("30.5"))

    def test_market_codes_load_only_requested_source_rows_without_membership(self):
        import json
        from market import DataBus
        from protocol import DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003", "600004"))
            registry_path = root / "data/source/registry/sources.json"
            registry = json.loads(registry_path.read_text())
            registry["markets"][0]["members"] = None
            registry_path.write_text(json.dumps(registry))
            bad = root / "data/source/fixture/factors/adjust_sh.600004_1990-01-01_2020-02-05.json"
            bad.write_text("broken unrelated factor")
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("open", "high", "low", "close", "volume", "amount"), 20,
                              "adjusted", market={"scope": "CN_A", "codes": ["600003"],
                                                  "filters": [{"field": "close", "op": "gte", "value": Decimal("10")}]})
            dataset = DataBus(DataStore(root)).query(query)
            self.assertEqual(dataset.scope["symbols"], ("600003",))
            self.assertEqual(tuple(dataset.records[-1].frame.fields), ("600003",))
            with self.assertRaisesRegex(DataError, "factor"):
                DataBus(DataStore(root)).query(DataQuery(
                    "fixture", date(2020, 1, 31), date(2020, 2, 4),
                    ("open", "high", "low", "close", "volume", "amount"), 20,
                    "adjusted", market={"scope": "CN_A"}))

    def test_adjusted_query_does_not_validate_requested_symbol_before_warmup(self):
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path)
            rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] < "2020-01-03":
                    row["close"] = -1.0
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)

            dataset = query(root, history=3)

            self.assertEqual(dataset.records[0].trade_date, date(2020, 1, 3))
            self.assertEqual(sum(record.trade_date < date(2020, 1, 31)
                                 for record in dataset.records), 20)

    def test_adjusted_query_does_not_read_unrequested_symbol_factor_cache(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003", "600004"))
            unrelated = root / "data/source/fixture/factors/adjust_sh.600004_1990-01-01_2020-02-05.json"
            unrelated.write_text("not a factor cache")

            dataset = query(root, symbols=("600003",), history=3)

            self.assertEqual(dataset.scope["symbols"], ("600003",))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_query_does_not_require_supplier_raw_files(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            raw = root / "data/source/fixture/provider/adjust_sh.600003.json"
            raw.parent.mkdir()
            raw.write_text('{"provider":"raw"}')
            raw.unlink()

            dataset = query(root, symbols=("600003",), history=3)

            self.assertEqual(dataset.scope["symbols"], ("600003",))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_weekend_query_end_resolves_to_last_session(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            dataset = query(root, history=3, start=date(2020, 1, 31), end=date(2020, 2, 2))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 1, 31))
            self.assertEqual(dataset.calendar[-1], date(2020, 2, 3))

    def test_calendar_coverage_must_cover_selected_warmup_and_sentinel(self):
        for bound, value in (("start", "2020-01-30"), ("end", "2020-02-04")):
            with self.subTest(bound=bound), TemporaryDirectory() as temp:
                root = Path(temp)
                make_snapshot(root, symbols=("600003",))
                path = root / "data/source/fixture/calendar.json"
                payload = json.loads(path.read_text())
                payload["coverage"][0][bound] = value
                path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(DataError, "calendar coverage"):
                    query(root, history=3)

    def test_calendar_coverage_can_start_after_unused_calendar_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/calendar.json"
            value = json.loads(path.read_text())
            value["coverage"][0]["start"] = "2019-12-01"
            path.write_text(json.dumps(value))
            dataset = query(root, history=3)
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_reversed_market_symbols_keep_price_and_extra_fields_aligned(self):
        from tests.history_fixture import update_manifest
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003", "600004"))
            update_manifest(root, fields={"amount_alias": {"column": "amount", "unit": "CNY"}})
            dataset = query(root, symbols=("sh.600004", "sh.600003"),
                            fields=FIELDS + ("amount_alias",), history=3)
            record = dataset.records[-1]
            self.assertEqual(tuple(record.frame.fields), ("sh.600004", "sh.600003"))
            self.assertEqual(record.frame.fields["sh.600004"]["close"], 12.0)
            self.assertEqual(record.frame.fields["sh.600004"]["amount_alias"], 64_000_000.0)
            self.assertEqual(record.frame.fields["sh.600003"]["amount_alias"], 63_000_000.0)

    def test_calendar_sentinel_does_not_require_a_future_price_bar(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            dataset = query(root, history=3)
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))
            self.assertEqual(dataset.calendar[-1], date(2020, 2, 5))
            self.assertNotIn(date(2020, 2, 5), {record.trade_date for record in dataset.records})

    def test_cash_dividend_identity_comes_from_source_plan_dates(self):
        from strategy import canonical_hash
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/corp/dividend_sh.600003_2020.json"
            value = json.loads(path.read_text())
            value["records"] = [{"announce_date":"2020-01-20", "registration_date":"2020-01-30",
                "operation_date":"2020-01-31", "payment_date":"2020-02-04",
                "stock_market_date":None, "cash_per_share":0.25, "stock_dividend_per_share":0,
                "reserve_to_stock_per_share":0, "cash_after_tax_text":None}]
            value["source_hash"] = canonical_hash(value["records"])
            path.write_text(json.dumps(value))

            dataset = query(root, history=3)
            actions = [action for record in dataset.records for action in record.registrations]

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].distribution_id, "600003:2020-01-20:2020-01-30")
            self.assertEqual(actions[0].event.cash_per_share, 0.25)

    def test_market_qualified_cash_action_matches_qualified_holding_code(self):
        from strategy import canonical_hash
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/corp/dividend_sh.600003_2020.json"
            value = json.loads(path.read_text())
            value["records"] = [{"announce_date":"2020-01-20", "registration_date":"2020-01-30",
                "operation_date":"2020-01-31", "payment_date":"2020-02-04",
                "stock_market_date":None, "cash_per_share":0.25, "stock_dividend_per_share":0,
                "reserve_to_stock_per_share":0, "cash_after_tax_text":None}]
            value["source_hash"] = canonical_hash(value["records"])
            path.write_text(json.dumps(value))
            dataset = query(root, symbols=("sh.600003",), history=3)
            actions = [action for record in dataset.records for action in record.registrations]
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].event.code, "sh.600003")

    def test_noncash_action_requires_a_valid_sourced_credit_date(self):
        from strategy import canonical_hash
        from tests.history_fixture import update_manifest
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_snapshot(root, symbols=("600003",))
            path = root / "data/source/fixture/corp/dividend_sh.600003_2020.json"
            value = json.loads(path.read_text())
            value["records"] = [{"announce_date":"2020-01-20", "registration_date":"2020-01-30",
                "operation_date":"2020-01-31", "payment_date":"2020-02-04",
                "stock_market_date":"2020-02-03", "cash_per_share":0, "stock_dividend_per_share":0.1,
                "reserve_to_stock_per_share":0, "cash_after_tax_text":None}]
            value["source_hash"] = canonical_hash(value["records"])
            path.write_text(json.dumps(value))
            policy_path = root / "data/source/fixture/account_policy.json"
            policy = json.loads(policy_path.read_text())
            policy["tax"]["bonus_holding_period"] = "originating_lot"
            policy["tax"]["bonus_tax_allocation"] = "proportional_entitlement"
            policy_path.write_text(json.dumps(policy))
            name = "corp/dividend_sh.600003_2020.json"
            update_manifest(root, inputs={"calendar": "calendar.json",
                "bars": "daily", "code_changes": "code_changes.json",
                "adjustments": {"template": "factors/adjust_{symbol}_{start}_{end}.json",
                                 "start": "1990-01-01", "end": "2020-02-05"},
                "distributions": {"template": "corp/dividend_{symbol}_{year}.json"},
                "securities": {"template": "basic/stock_basic_{symbol}.json"},
                "account_policy": "account_policy.json", "action_facts": [{
                    "source_file": name, "source_row": 0, "credit_date": "2020-02-03",
                    "stock_nominal_per_share": "1", "reserve_taxable_per_share": "0",
                    "tax_source_file": "policy_source.json",
                }]})
            with self.assertRaisesRegex(DataError, "credit source"):
                query(root, history=3)

            source = root / "data/source/fixture/credit.json"
            source.write_text('{"kind":"verified_credit"}')
            manifest = json.loads((root / "data/source/fixture/manifest.json").read_text())
            manifest["inputs"]["action_facts"][0]["credit_source_file"] = "credit.json"
            manifest["inputs"]["action_facts"][0]["credit_date"] = "2020-02-30"
            (root / "data/source/fixture/manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DataError, "credit date is invalid"):
                query(root, history=3)
            manifest["inputs"]["action_facts"][0]["credit_date"] = "2020-02-03"
            (root / "data/source/fixture/manifest.json").write_text(json.dumps(manifest))
            dataset = query(root, history=3)
            actions = [action for record in dataset.records for action in record.registrations]
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].share_credit_date, date(2020, 2, 3))

    def test_raw_query_reuses_trade_state_validation(self):
        import pyarrow.parquet as pq
        from market import DataBus
        from protocol import DataQuery

        cases = ((0.0, 100.0, 1000.0, "contradictory_trade_state"),
                 (1.0, 0.0, 0.0, "contradictory_trade_state"),
                 (None, 0.0, 0.0, "unknown_trade_state"))
        for status, volume, amount, reason in cases:
            with self.subTest(status=status, volume=volume, amount=amount):
                with TemporaryDirectory() as temp:
                    root = Path(temp); make_snapshot(root)
                    path = root / "data/source/fixture/daily/bars.parquet"
                    table = pq.read_table(path); rows = table.to_pylist()
                    for row in rows:
                        if row["code"] == "600003" and row["date"] == "2020-01-31":
                            row.update(tradestatus=status, volume=volume, amount=amount)
                    pq.write_table(table.from_pylist(rows, schema=table.schema), path)
                    with self.assertRaisesRegex(ValueError, reason):
                        DataBus(DataStore(root)).query(DataQuery(
                            "fixture", date(2020, 1, 31), date(2020, 1, 31),
                            ("open", "high", "low", "close", "volume", "amount"),
                            1, "raw", symbols=("sh.600003",), universe="CN_A_SHARE"))

    def test_raw_query_keeps_a_zero_turnover_suspension_as_valuation_only(self):
        import pyarrow.parquet as pq
        from market import DataBus
        from protocol import DataQuery

        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path); rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] == "2020-01-31":
                    row.update(tradestatus=0.0, volume=0.0, amount=0.0)
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            dataset = DataBus(DataStore(root)).query(DataQuery(
                "fixture", date(2020, 1, 31), date(2020, 1, 31),
                ("open", "high", "low", "close", "volume", "amount"),
                1, "raw", symbols=("sh.600003",), universe="CN_A_SHARE"))
            record = dataset.records[0]
            self.assertNotIn("600003", record.bars)
            self.assertEqual(record.frame.fields["sh.600003"]["close"], 11.0)

    def test_history_day_exposes_prior20_raw_volume_and_empty_first_value(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.records[0].opening_volume20, {})
            volumes = [record.bars["600003"].volume for record in dataset.records[:20]]
            self.assertEqual(dataset.records[20].opening_volume20["600003"], Decimal(str(sum(volumes) / 20)))

    def test_adjusted_query_normalizes_a_market_qualified_symbol(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            dataset = query(root, symbols=("sh.600003",))
            self.assertEqual(dataset.scope["symbols"], ("sh.600003",))
            self.assertIn("sh.600003", dataset.records[-1].bars)

    def test_history_day_keeps_amount_liquidity_separate_from_volume_liquidity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            amount = [record.bars["600003"].amount for record in dataset.records[:20]]
            volume = [record.bars["600003"].volume for record in dataset.records[:20]]
            self.assertEqual(dataset.records[20].opening_amount20["600003"], Decimal(str(sum(amount) / 20)))
            self.assertEqual(dataset.records[20].opening_volume20["600003"], Decimal(str(sum(volume) / 20)))
            self.assertNotEqual(dataset.records[20].opening_amount20["600003"], dataset.records[20].opening_volume20["600003"])

    def test_suspension_keeps_last_amount_and_volume_liquidity_for_resume(self):
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"; table = pq.read_table(path)
            rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] == "2020-01-31":
                    row.update(tradestatus=0.0, volume=0.0, amount=0.0)
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            dataset = query(root); by_day = {record.trade_date: record for record in dataset.records}
            suspended, resumed = by_day[date(2020, 1, 31)], by_day[date(2020, 2, 3)]
            previous = [record for record in dataset.records if record.trade_date < date(2020, 1, 31)
                        and record.bars["600003"].volume > 0]
            self.assertEqual(suspended.bars["600003"].volume, 0.0)
            self.assertEqual(suspended.bars["600003"].amount, 0.0)
            self.assertEqual(resumed.opening_amount20["600003"], Decimal(str(sum(r.bars["600003"].amount for r in previous[-20:]) / 20)))
            self.assertEqual(resumed.opening_volume20["600003"], Decimal(str(sum(r.bars["600003"].volume for r in previous[-20:]) / 20)))
            request=DataQuery('fixture',date(2020,1,31),date(2020,2,4),FIELDS,3,'adjusted',
                              symbols=('600003',),tax='none',accounting='research')
            research=DataBus(DataStore(root)).query(request)
            closed=next(r for r in research.records if r.trade_date==date(2020,1,31))
            self.assertNotIn('600003',closed.bars)
            self.assertNotIn('600003',closed.opening_marks)
            self.assertNotIn('600003',closed.closing_marks)

    def test_history_context_exposes_only_liquidity_input_basis(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.provenance["query_semantics"], "snapshot_replay")
            self.assertNotIn("execution", dataset.provenance)
            self.assertNotIn("execution_hash", dataset.provenance)

    def test_volume_warmup_rejects_missing_observations_before_query(self):
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"; table = pq.read_table(path); rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] < "2020-01-31": row["volume"] = None
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            with self.assertRaisesRegex(ValueError, "volume"): query(root)

    def test_preflight_effective_fields_match_resolved_dataset_fields(self):
        from backend import KernelBacktestPort
        from tests.fee_fixture import configured_settlement
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            port = KernelBacktestPort(DataBus(DataStore(root)), configured_settlement())
            result = port.handle("backtest.prepare", payload(
                module="plugins.strategies.moving_average_cross",
                params={"fast_window": 2, "slow_window": 3},
                market={"scope": "CN_A", "codes": ["600000"]},
                fields=["signal_close"], history=3))
            self.assertFalse(result["issues"])
            self.assertEqual(result["requirements"]["fields"], ["signal_close"])
            self.assertIn("signal_close", result["effective_config"]["fields"])
            self.assertEqual(result["effective_config"]["market"]["codes"], ["600000"])
            self.assertNotIn("symbols", result["effective_config"])
            self.assertNotIn("universe", result["effective_config"])

    def test_real_snapshot_without_universe_source_file_is_readable(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.scope["symbols"], ("600003",))
            self.assertNotIn("universe_source", dataset.scope)

    def test_verified_boolean_does_not_authorize_a_dataset(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); folder = root / "data/source/sample"; folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps({"verified": True, "schema": 1}))
            with self.assertRaises(DataError):
                DataBus(DataStore(root)).query(DataQuery("sample", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted"))

    def test_unknown_dataset_is_explicit_failure(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(DataError):
                DataBus(DataStore(Path(temp))).query(DataQuery("missing", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted"))

    def test_real_files_execute_parameter_ab_with_one_strategy_file(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            port = KernelBacktestPort(DataBus(DataStore(root)), Settlement)
            a = port.handle("backtest.run", payload(run_id="0", params={"positions": 4})).data
            b = port.handle("backtest.run", payload(
                run_id="1", params={"positions": 8},
                execution={**payload()["execution"], "price": {"model": "ratio", "params": {"rate": ".002", "tick": ".01"}}},
            )).data
            self.assertTrue(a["trades"]); self.assertTrue(b["trades"])
            self.assertEqual(a["data_hash"], b["data_hash"])
            self.assertNotEqual(a["execution_hash"], b["execution_hash"])
            self.assertNotEqual(a["metrics"]["final_equity"], b["metrics"]["final_equity"])

    def test_parameter_change_uses_same_account_policy(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            result = KernelBacktestPort(DataBus(DataStore(root)), Settlement).handle("backtest.prepare", payload(params={"positions": 8}))
            self.assertFalse(result["issues"])

    def test_long_history_stays_bounded_and_trace_does_not_duplicate_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root, history_start=date(2018, 1, 1)); dataset = query(root)
            self.assertEqual(len(dataset.records), 23)
            self.assertEqual(dataset.records[0].trade_date, date(2020, 1, 3))
            self.assertLessEqual(max(len(rows) for record in dataset.records for rows in record.frame.history.values()), 20)

    def test_market_source_cannot_be_excluded_using_account_role(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(path.read_text()); manifest["files"][0]["role"] = "account"; path.write_text(json.dumps(manifest))
            self.assertEqual(query(root).dataset, "fixture")

    def test_unseen_strategy_requests_subset_and_only_declared_fields(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root, history=17)
            self.assertEqual(tuple(dataset.scope["symbols"]), ("600003",))
            self.assertTrue(all(set(row.fields) == set(FIELDS) for row in dataset.records[-1].frame.history["600003"]))

    def test_declared_arbitrary_numeric_field_is_aligned_and_not_substituted(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path).append_column("arbitrary_value", pa.array([i + 0.5 for i in range(pq.read_table(path).num_rows)]))
            pq.write_table(table, path); manifest_path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(manifest_path.read_text()); manifest["fields"] = {"quality": {"column": "arbitrary_value", "unit": "研究分数"}}
            manifest_path.write_text(json.dumps(manifest)); dataset = query(root, fields=("quality",))
            self.assertIsInstance(dataset.records[-1].frame.fields["600003"]["quality"], float)

    def test_market_universe_requirement_rejects_fixed_dataset_and_reserved_fields(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/manifest.json"; manifest = json.loads(path.read_text())
            manifest["scope"]["universe"] = "fixed_symbols"; path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "universe_scope_mismatch"):
                query(root, symbols=None, universe="CN_A_SHARE")
            manifest["scope"]["universe"] = "CN_A_SHARE"; manifest["fields"] = {"signal_close": {"column": "close", "unit": "元"}}
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "reserved"): query(root)

    def test_execution_history_requirement_is_checked_before_short_strategy_run(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root, history_start=date(2020, 1, 1))
            with self.assertRaisesRegex(ValueError, "requires 20"): query(root, history=2, start=date(2020, 1, 10))


if __name__ == "__main__":
    unittest.main()
