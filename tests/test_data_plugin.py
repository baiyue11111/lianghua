import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
import pyarrow as pa

from market import DataBus
from market.views import Views
from plugins.data import Adapter, Scaler
from protocol import (DataError, DataQuery, Dataset, Filter, HistoryDay, MarketFrame,
                      SQLQuery, TableDef, TableQuery, TableWrite)
from storage import DataStore


class DuckLakeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = DataBus(DataStore(Path(self.folder.name)))
        self.addCleanup(self.data.close)
        self.data.define(TableDef('bars', {'code': 'VARCHAR'}, ('code',)))
        self.data.save_manifest('fixture', {
            'schema': 3, 'scope': {'universe': 'TEST', 'symbols': ['600003', '600004']},
            'fields': {}, 'calendar': 'calendar:fixture',
            'inputs': {'bars': 'lake:bars', 'calendar': 'calendar:fixture'},
        })
        self.binding = {'source': 'components', 'code': 'code', 'index': 'index_id',
                        'effective': 'effective', 'known': 'known', 'included': 'included'}
        self.entry = {'scope': 'TEST', 'dataset_id': 'fixture',
                      'calendar': 'calendar:fixture', 'currency': 'CNY',
                      'execution': 'cn_a_share', 'timezone': 'Asia/Shanghai',
                      'session_open': '09:30', 'session_close': '15:00',
                      'code_normalizer': 'normalize_security_id',
                      'code_pattern': r'sh\.60\d{4}', 'members': self.binding, 'fields': {}}
        self.members = [
            {'code': '600003', 'index_id': 'I', 'effective': date(2020, 1, 1),
             'known': datetime.fromisoformat('2020-01-01T08:00:00+08:00'), 'included': True},
            {'code': '600004', 'index_id': 'I', 'effective': date(2020, 1, 2),
             'known': datetime.fromisoformat('2020-01-02T16:00:00+08:00'), 'included': True},
        ]
        self.data.define(TableDef('members', {
            'code': 'VARCHAR', 'index_id': 'VARCHAR', 'effective': 'DATE',
            'known': 'TIMESTAMPTZ', 'included': 'BOOLEAN'},
            ('code', 'index_id', 'effective', 'known')))
        self.data.write(TableWrite('members', pa.Table.from_pylist(self.members)))
        self.data.bind('components', 'records', 'lake:members')
        self.register()

    def register(self):
        self.data.update_registry(lambda value: {**value, 'markets': [self.entry]})

    def query(self, **market):
        return DataQuery('fixture', date(2020, 1, 2), date(2020, 1, 3),
                         ('close',), 1, 'raw', market={'scope': 'TEST', **market})

    def dataset(self):
        records = []
        for day in (date(2020, 1, 2), date(2020, 1, 3)):
            frame = MarketFrame(datetime.fromisoformat(f'{day}T15:00:00+08:00'), 'TEST',
                                {code: {'close': 10.0} for code in ('600003', '600004')},
                                history={'600003': (), '600004': ()})
            records.append(HistoryDay(day, frame, {}, {}, {}, {}, {}, {},
                                      date(2020, 1, 1), ()))
        return Dataset('fixture', tuple(records), {}, {},
                       {'universe': 'TEST', 'symbols': ('600003', '600004')},
                       (date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)), ('close',), 1)

    def test_candidates_read_native_membership_and_new_publications(self):
        request = self.query(index='I')
        args = ('fixture', request.start, request.end)
        self.assertEqual(Adapter.candidates(self.data, request.market, *args), ('600003', '600004'))
        self.assertEqual(Adapter.candidates(self.data, request.market, 'fixture',
                                           date(2020, 1, 1), date(2020, 1, 1)), ('600003',))
        self.data.write(TableWrite('members', pa.Table.from_pylist([{
            **self.members[1], 'effective': date(2020, 1, 1),
            'known': datetime.fromisoformat('2020-01-01T08:00:00+08:00')}])))
        self.assertEqual(Adapter.candidates(self.data, request.market, 'fixture',
                                           date(2020, 1, 1), date(2020, 1, 1)), ('600003', '600004'))

    def test_candidates_keep_registered_scope_pattern_and_requested_order(self):
        args = ('fixture', date(2020, 1, 2), date(2020, 1, 3))
        self.assertEqual(Adapter.candidates(self.data, {'scope': 'TEST'}, *args), ('600003', '600004'))
        self.assertEqual(Adapter.candidates(self.data, {'scope': 'TEST', 'index': 'I',
                                                       'codes': ['sh.600004', '600003']}, *args),
                         ('600004', '600003'))
        for market in ({'scope': 'MISSING'}, {'scope': 'TEST', 'codes': ['sh.000300']}):
            with self.subTest(market=market), self.assertRaises(ValueError):
                Adapter.candidates(self.data, market, *args)

    def test_membership_is_visible_only_after_publication(self):
        result = Adapter.market(self.data, self.query(index='I'), self.dataset())
        self.assertEqual(tuple(result.records[0].frame.fields), ('600003',))
        self.assertEqual(result.records[0].opening_eligible, ('600003',))
        self.assertEqual(tuple(result.records[1].frame.fields), ('600003', '600004'))
        self.assertEqual(result.records[1].opening_eligible, ('600003', '600004'))
        self.assertEqual(result.provenance['market']['source_hashes']['lake:members'],
                         self.data.evidence('lake:members'))

    def test_market_rejects_missing_membership_and_readiness(self):
        for changes, message in (({'members': None}, 'membership'),
                                 ({'calendar': 'calendar:other'}, 'not ready'),
                                 ({'currency': ''}, 'not ready'),
                                 ({'code_normalizer': 'unknown'}, 'not registered')):
            with self.subTest(changes=changes):
                self.entry.update(changes)
                self.register()
                with self.assertRaisesRegex(DataError, message):
                    Adapter.market(self.data, self.query(index='I'), self.dataset())
                self.entry.update(members=self.binding, calendar='calendar:fixture', currency='CNY',
                                  code_normalizer='normalize_security_id')

    def test_candidates_compare_native_timestamp_in_market_timezone(self):
        self.data.write(TableWrite('members', pa.Table.from_pylist([{
            **self.members[1], 'index_id': 'NIGHT',
            'known': datetime.fromisoformat('2020-01-02T16:30:00+00:00')}])))
        self.assertEqual(Adapter.candidates(self.data, self.query(index='NIGHT').market,
                                           'fixture', date(2020, 1, 2), date(2020, 1, 2)), ())
        self.assertEqual(Adapter.candidates(self.data, self.query(index='NIGHT').market,
                                           'fixture', date(2020, 1, 2), date(2020, 1, 3)), ('600004',))

    def test_old_file_bindings_are_rejected_without_fallback(self):
        self.entry['members'] = {**self.binding, 'source': 'old', 'file': 'records.json'}
        self.register()
        with self.assertRaises(ValueError):
            Adapter.candidates(self.data, self.query(index='I').market,
                               'fixture', date(2020, 1, 2), date(2020, 1, 3))
        with self.assertRaises(DataError):
            Adapter.market(self.data, self.query(index='I'), self.dataset())

    def test_native_facts_keep_field_code_binding_units_and_revision_visibility(self):
        self.data.define(TableDef('facts', {
            'roe_code': 'VARCHAR', 'book_code': 'VARCHAR', 'period': 'DATE',
            'published': 'TIMESTAMPTZ', 'roe': 'DECIMAL(10,4)', 'book': 'DECIMAL(10,4)'},
            ('roe_code', 'book_code', 'period', 'published')))
        rows = [
            {'roe_code': '600003', 'book_code': '600004', 'period': date(2019, 9, 30),
             'published': datetime.fromisoformat('2020-01-01T08:00:00+08:00'),
             'roe': Decimal('.2'), 'book': Decimal('10')},
            {'roe_code': '600004', 'book_code': '600003', 'period': date(2019, 9, 30),
             'published': datetime.fromisoformat('2020-01-01T08:00:00+08:00'),
             'roe': Decimal('.3'), 'book': Decimal('20')},
        ]
        self.data.write(TableWrite('facts', pa.Table.from_pylist(rows)))
        self.data.bind('fundamentals', 'records', 'lake:facts')
        self.entry['fields'] = {
            name: {'source': 'facts', 'binding': {'source': 'fundamentals'}, 'code': name + '_code',
                   'effective': 'period', 'known': 'published', 'column': name, 'type': 'number',
                   'unit': unit} for name, unit in (('roe', 'ratio'), ('book', 'CNY'))}
        self.register()
        request = self.query(filters=[{'field': 'roe', 'op': 'gt', 'value': .1},
                                      {'field': 'book', 'op': 'gt', 'value': 15}])
        before = Adapter.market(self.data, request, self.dataset())
        self.assertEqual([tuple(record.frame.fields) for record in before.records],
                         [('600003',), ('600003',)])
        self.data.write(TableWrite('facts', pa.Table.from_pylist([{
            **rows[0], 'published': datetime.fromisoformat('2020-01-02T16:00:00+08:00'),
            'roe': Decimal('.05')}])))
        after = Adapter.market(self.data, request, self.dataset())
        self.assertEqual(tuple(after.records[0].frame.fields), ('600003',))
        self.assertEqual(tuple(after.records[1].frame.fields), ())
        self.assertEqual(self.data.registry()['markets'][0]['fields']['roe']['unit'], 'ratio')
        self.assertEqual(after.provenance['market']['source_hashes']['lake:facts'],
                         self.data.evidence('lake:facts'))


class DuckLakeViewsTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = DataBus(DataStore(Path(self.folder.name)))
        self.addCleanup(self.data.close)
        self.data.define(TableDef('bars', {'code': 'VARCHAR'}, ('code',)))
        self.data.save_manifest('fixture', {
            'schema': 3, 'scope': {'universe': 'TEST', 'symbols': ['600003', '600004']},
            'fields': {}, 'calendar': 'calendar:fixture',
            'inputs': {'bars': 'lake:bars', 'calendar': 'calendar:fixture'},
        })

    def view(self, rows):
        self.data.put_rows('daily_prices', rows, keys=('code', 'date'), types={'close_hfq': 'DOUBLE'})
        self.data.bind('daily', 'bars', 'lake:daily_prices')
        self.data.put_rows('stock_records', [{
            'code': '600003', 'name': '招商银行', 'exchange': 'SH', 'status': 'listed',
            'first_date': date(2002, 4, 9), 'last_date': None}], keys=('code',), types={'last_date': 'DATE'})
        self.data.bind('stocks', 'records', 'lake:stock_records')
        for identity in ('valuation', 'fundamentals'):
            self.data.put_rows(identity, [{'code': '600003', 'quarter': date(2019, 9, 30),
                                          '中文字段': Decimal('1.25')}], keys=('code', 'quarter'))
            self.data.bind(identity, 'records', 'lake:' + identity)
        self.data.define(TableDef('changes', {'id': 'VARCHAR', 'factor_basis_continuity_verified': 'BOOLEAN'}, ('id',)))
        manifest = self.data.manifest('fixture')
        manifest['inputs']['code_changes'] = 'changes:changes'
        self.data.save_manifest('fixture', manifest)
        self.data.update_registry(lambda value: {**value, 'datasets': [{'id': 'fixture'}],
            'sources': [{'id': identity, 'data_type': kind, 'default': True, 'modes': ['fix']}
                        for identity, kind in (('daily', 'daily'), ('stocks', 'stock_info'),
                                               ('valuation', 'valuation'), ('fundamentals', 'fundamentals'))]})
        return Views(self.data, Mock(spec=['select']))

    def test_views_reports_compute_native_facts_and_keep_response_shapes(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0,
               'tradestatus': 1, 'close_hfq': 10.0}
        view = self.view([row])
        status = view.handle('data.status', {})
        self.assertEqual(status['总览']['日线行情']['股票数'], 1)
        self.assertTrue(status['结论']['回测可用'])
        self.assertEqual(status['基本面因子字段']['中文字段']['coverage'], 100.0)
        report = view.handle('data.research_inputs', {})
        self.assertEqual(report['summary'], {
            'first_date': '2020-01-02', 'last_date': '2020-01-02', 'rows': 1, 'symbols': 1,
            'adjusted_close_rows': 1, 'invalid_price_rows': 0})
        digest = report['partition_sha256']['lake:daily_prices']
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertEqual(view.source_facts()['partition_sha256']['lake:daily_prices'], digest)
        self.assertEqual(report['ranking_status'], 'NOT_RELEASED')
        self.assertEqual(view.handle('data.check', {}), {'items': []})

    def test_views_evidence_digest_is_order_independent_and_changes_with_data(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0, 'tradestatus': 1}
        rows = [row, {**row, 'date': date(2020, 1, 3)}]
        view = self.view(rows)
        before = view.source_facts()['partition_sha256']
        self.data.delete(TableQuery('daily_prices'))
        self.data.put_rows('daily_prices', list(reversed(rows)), keys=('code', 'date'))
        self.assertEqual(view.source_facts()['partition_sha256'], before)
        self.data.put_rows('daily_prices', [{**row, 'date': date(2020, 1, 6)}], keys=('code', 'date'))
        self.assertNotEqual(view.source_facts()['partition_sha256'], before)

    def test_views_reports_expose_price_and_turnover_errors(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 12.0, 'close': 10.0, 'volume': 100.0, 'amount': 100.0,
               'tradestatus': 0, 'close_hfq': None}
        view = self.view([row])
        report = view.handle('data.research_inputs', {})
        self.assertEqual(report['summary']['invalid_price_rows'], 1)
        self.assertEqual(report['trade_state_issues']['suspended_with_turnover'], 1)
        self.assertEqual(report['price_basis_issues'][0]['median_vwap_close_ratio'], .1)
        self.assertFalse(view.handle('data.status', {})['结论']['回测可用'])
        repairs = view.handle('data.check', {})['items']
        self.assertTrue(any(item['fix'] and item['fix']['source_id'] == 'daily' for item in repairs))

    def test_views_search_uses_mapped_records_and_filtered_latest_daily_fact(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 100000000.0,
               'tradestatus': 1, 'pctChg': 1.25}
        view = self.view([row, {**row, 'date': date(2020, 1, 3), 'close': 12.0}])
        result = view.handle('data.search', {'q': '招商'})
        self.assertEqual(result['items'], [{
            '代码': '600003', '名称': '招商银行', '交易所': 'SH', '状态': '现存',
            '首日': date(2002, 4, 9), '末日': None, '最新收盘': 12.0,
            '涨跌幅': 1.25, '成交额亿': 1.0, '行情日期': '2020-01-03'}])
        self.assertEqual(view.search('不存在'), {'items': []})
        with self.assertRaises(TypeError):
            view.search(None)

    def test_views_registered_missing_source_mapping_is_an_error(self):
        self.data.update_registry(lambda value: {**value, 'sources': [{'id': 'missing', 'data_type': 'daily'}]})
        with self.assertRaisesRegex(DataError, 'not registered'):
            Views(self.data, Mock()).source_facts()

    def test_views_source_facts_use_storage_identity_without_exporting_detail(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0,
               'tradestatus': 1, 'close_hfq': 10.0, 'unused_payload': 'x' * 10000}
        view = self.view([row])
        self.data.bind('cn_a', 'bars', 'lake:daily_prices')
        self.data.delete(TableQuery('source_tables', (Filter('identity', 'eq', 'daily'),)))
        self.data.update_registry(lambda value: {**value, 'sources': [
            {**source, 'storage_id': 'cn_a'} if source['id'] == 'daily' else source
            for source in value['sources']]})
        scanned = []
        scan = self.data.scan
        def record(query):
            result = scan(query)
            if isinstance(query, SQLQuery):
                scanned.append((query.sql, result.table.num_rows))
            return result
        with patch.object(self.data, 'table', side_effect=AssertionError('raw table export')), \
                patch.object(self.data, 'evidence', side_effect=AssertionError('raw evidence export')), \
                patch.object(self.data, 'scan', side_effect=record), \
                patch.object(self.data, 'transaction', wraps=self.data.transaction) as transaction:
            self.assertEqual(view._tables('daily'), ('lake:daily_prices',))
            facts = view.source_facts()
        transaction.assert_called_once_with(readonly=True)
        self.assertEqual(facts['daily']['rows'], 1)
        self.assertEqual(facts['daily']['field_coverage']['close'], 100.0)
        self.assertEqual(facts['errors'], [])
        self.assertTrue(scanned)
        self.assertTrue(all(count <= 20 for _, count in scanned))

    def test_views_source_facts_keep_sql_summary_and_diagnostic_semantics(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0,
               'tradestatus': 1, 'close_hfq': 10.0}
        view = self.view([row, {**row, 'date': date(2020, 1, 3), 'amount': None},
                          {**row, 'code': '600004', 'tradestatus': 0},
                          {**row, 'code': '600005', 'volume': 0.0, 'amount': 0.0},
                          {**row, 'code': '600006', 'tradestatus': None,
                           'volume': 0.0, 'amount': 0.0}])
        facts = view.source_facts()
        daily = facts['daily']
        self.assertEqual((daily['rows'], daily['symbols']), (5, 4))
        self.assertEqual(daily['missing_fields']['amount'], 1)
        self.assertEqual(daily['field_coverage']['amount'], 80.0)
        self.assertEqual(daily['field_symbols']['amount'], 4)
        self.assertEqual(daily['trade_state_issues'], {
            'suspended_with_turnover': 1, 'trading_without_turnover': 2, 'unknown_zero_turnover': 1})
        self.assertEqual(daily['latest_stock_count'], 1)
        self.assertEqual(daily['tracked_stock_count'], 4)
        self.assertEqual(daily['median_stock_date'], '2020-01-02')
        self.assertEqual(daily['yearly'], {'2020': {'rows': 5, 'symbols': 4}})
        self.assertEqual(facts['fundamentals']['field_stats']['中文字段']['non_null'], 1)

    def test_views_source_facts_find_duplicates_and_union_missing_columns(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0, 'tradestatus': 1}
        view = self.view([row])
        self.data.put_rows('other_prices', [{key: value for key, value in row.items() if key != 'amount'}],
                           keys=('code', 'date'))
        self.data.bind('other', 'bars', 'lake:other_prices')
        self.data.update_registry(lambda value: {**value, 'sources': [*value['sources'],
            {'id': 'another_daily', 'storage_id': 'other', 'data_type': 'daily'}]})
        facts = view.source_facts()
        self.assertEqual(facts['daily']['duplicate_keys'], 1)
        self.assertEqual(facts['daily']['missing_fields']['amount'], 1)
        self.assertEqual(facts['daily']['field_coverage']['amount'], 50.0)
        self.assertTrue(any('重复键' in error for error in facts['errors']))

    def test_views_reports_do_not_treat_empty_registry_as_ready(self):
        view = Views(self.data, Mock())
        with patch.object(self.data, 'table', side_effect=AssertionError('raw table export')):
            facts = view.source_facts()
            status = view.profile_report()
        self.assertEqual(facts['daily']['rows'], 0)
        self.assertEqual(facts['facts']['files'], 0)
        self.assertEqual(facts['code_change_status'], 'unknown')
        self.assertFalse(status['结论']['回测可用'])
        self.assertTrue(facts['errors'])

    def test_views_source_facts_preserve_bounded_fact_validation(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 1000.0, 'tradestatus': 1}
        view = self.view([row])
        self.data.put_rows('fact_coverage', [
            {'reference': f'invalid:{i:02d}', 'kind': 'adjust', 'code': '600003',
             'start': None, 'end': None, 'year': None, 'source_hash': '0' * 64}
            for i in range(20)], keys=('reference',))
        with patch.object(self.data, 'object', wraps=self.data.object) as read:
            facts = view.source_facts()
        checked = [call.args[0] for call in read.call_args_list if call.args[0].startswith('invalid:')]
        self.assertEqual(len(checked), 16)
        self.assertEqual(checked, [f'invalid:{i:02d}' for i in [*range(8), *range(12, 20)]])
        self.assertEqual(facts['facts'], {'files': 20, 'checked': 16, 'valid': 0,
                                         'invalid': 16, 'check_scope': 'sample'})
        self.assertEqual(facts['saved_factor_files'], 20)
        self.assertTrue(any('抽样检查 16/20' in error for error in facts['errors']))

    def test_views_search_limits_filtered_latest_rows_in_database(self):
        row = {'code': '600003', 'date': date(2020, 1, 2), 'open': 10.0, 'high': 11.0,
               'low': 9.0, 'close': 10.0, 'volume': 100.0, 'amount': 100000000.0,
               'tradestatus': 1, 'pctChg': 1.25}
        view = self.view([row])
        stocks = [{'code': str(600100 + i), 'name': '招商' + str(i), 'exchange': 'SH',
                   'status': 'listed', 'first_date': date(2000, 1, 1), 'last_date': None}
                  for i in range(30)]
        self.data.put_rows('stock_records', stocks, keys=('code',))
        from datetime import timedelta
        self.data.put_rows('daily_prices', [{**row, 'code': stock['code'],
            'date': date(2020, 1, 1) + timedelta(days=day), 'close': float(day + 1)}
            for stock in stocks for day in range(100)], keys=('code', 'date'))
        scan, counts = self.data.scan, []
        def record(query):
            result = scan(query)
            if isinstance(query, SQLQuery):
                counts.append(result.table.num_rows)
            return result
        with patch.object(self.data, 'table', side_effect=AssertionError('raw table export')), \
                patch.object(self.data, 'scan', side_effect=record), \
                patch.object(self.data, 'transaction', wraps=self.data.transaction) as transaction:
            items = view.search('招商')['items']
            self.assertEqual(view.search("' OR true --"), {'items': []})
        self.assertTrue(all(call.kwargs == {'readonly': True} for call in transaction.call_args_list))
        self.assertEqual(len(items), 20)
        self.assertEqual([item['代码'] for item in items], ['600003'] + [str(600100 + i) for i in range(19)])
        self.assertEqual(items[-1]['最新收盘'], 100.0)
        self.assertEqual(items[-1]['行情日期'], '2020-04-09')
        self.assertTrue(counts)
        self.assertLessEqual(max(counts), 20)


class MarketViewsTests(unittest.TestCase):
    def test_bars_use_dataset_calendar_and_standard_query(self):
        data = Mock(spec=['calendar', 'query'])
        sources = Mock(spec=['select'])
        sources.select.return_value = {'dataset_id': 'fixture', 'calendar': 'calendar:fixture'}
        data.calendar.return_value = {'sessions': ['2020-01-02', '2020-01-03', '2020-01-06']}
        frames = [MarketFrame(datetime.fromisoformat(day + 'T15:00:00+08:00'), 'TEST', {
            '600003': {'open': 10.0, 'high': 13.0, 'low': 9.0, 'close': 12.0,
                       'volume': 100, 'amount': 1200}})
                  for day in ('2020-01-02', '2020-01-03')]
        from types import SimpleNamespace
        data.query.return_value = SimpleNamespace(records=[
            SimpleNamespace(trade_date=frame.at.date(), frame=frame) for frame in frames])
        view = Views(data, sources)
        daily = view.bars({'code': '600003'})
        data.calendar.assert_called_once_with('fixture')
        request = data.query.call_args.args[0]
        self.assertIsInstance(request, DataQuery)
        self.assertEqual((request.start, request.end, request.symbols, request.price_basis),
                         (date(2020, 1, 2), date(2020, 1, 3), ('600003',), 'raw'))
        self.assertEqual(daily, {'dates': ['2020-01-02', '2020-01-03'],
                                 'candles': [[10.0, 12.0, 9.0, 13.0]] * 2,
                                 'volumes': [100, 100]})
        weekly = view.bars({'code': '600003', 'freq': '周', 'adj': '前复权'})
        self.assertEqual(data.query.call_args.args[0].price_basis, 'adjusted')
        self.assertEqual(weekly, {'dates': ['2020-01-03'],
                                  'candles': [[10.0, 12.0, 9.0, 13.0]], 'volumes': [200]})

    def test_missing_calendar_is_rejected(self):
        data = Mock(spec=['calendar'])
        data.calendar.return_value = {'sessions': ['2020-01-02']}
        sources = Mock(spec=['select'])
        sources.select.return_value = {'dataset_id': 'fixture', 'calendar': 'calendar:fixture'}
        with self.assertRaisesRegex(ValueError, 'calendar is missing'):
            Views(data, sources).bars({'code': '600003'})


class ScalerTests(unittest.TestCase):
    def test_fact_lookup_selects_latest_known_period_without_row_order_ties(self):
        from datetime import date, datetime
        from plugins.data import Adapter
        fields = {
            "roe": {"source": "facts", "code": "code", "effective": "report_date",
                    "known": "published_at", "column": "roe", "type": "number"},
            "label": {"source": "facts", "code": "code", "effective": "report_date",
                      "known": "published_at", "column": "label", "type": "string"},
        }
        rows = {
            "roe": [
                {"code": "A", "report_date": date(2019, 9, 30),
                 "published_at": "2020-01-30T16:00:00+08:00", "roe": .1},
                {"code": "A", "report_date": date(2019, 12, 31),
                 "published_at": "2020-01-31T16:00:00+08:00", "roe": .2},
                {"code": "A", "report_date": date(2019, 9, 30),
                 "published_at": "2020-02-04T16:00:00+08:00", "roe": .9},
            ],
            "label": [{"code": "A", "report_date": date(2019, 9, 30),
                       "published_at": datetime.fromisoformat("2020-01-30T16:00:00+08:00"), "label": True}],
        }
        lookup = Adapter.facts(fields, rows)
        self.assertIsNone(lookup("roe", "A", date(2020, 1, 30),
                                 datetime.fromisoformat("2020-01-30T15:00:00+08:00")))
        self.assertEqual(lookup("roe", "A", date(2020, 1, 31),
                                datetime.fromisoformat("2020-01-31T15:00:00+08:00")), .1)
        self.assertEqual(lookup("roe", "A", date(2020, 2, 3),
                                datetime.fromisoformat("2020-02-03T15:00:00+08:00")), .2)
        self.assertEqual(lookup("roe", "A", date(2020, 2, 4),
                                datetime.fromisoformat("2020-02-04T15:00:00+08:00")), .2)
        self.assertEqual(lookup("roe", "A", date(2020, 2, 5),
                                datetime.fromisoformat("2020-02-05T15:00:00+08:00")), .2)
        self.assertIs(lookup("label", "A", date(2020, 2, 3),
                             datetime.fromisoformat("2020-02-03T15:00:00+08:00")), True)

    def test_fact_lookup_rejects_conflicting_identity_and_naive_publication(self):
        from plugins.data import Adapter
        fields = {"roe": {"source": "facts", "code": "code", "effective": "period",
                           "known": "known", "column": "value", "type": "number"}}
        row = {"code": "A", "period": "2020-01-01", "known": "2020-01-02T12:00:00+00:00", "value": .1}
        for rows in ([row, dict(row)], [dict(row, known="2020-01-02T12:00:00")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                Adapter.facts(fields, {"roe": rows})

    def test_fact_lookup_keeps_an_explicit_missing_revision_and_no_record(self):
        from datetime import date, datetime
        from plugins.data import Adapter
        fields = {"roe": {"source": "facts", "code": "code", "effective": "period",
                           "known": "known", "column": "value", "type": "number"}}
        rows = [{"code": "A", "period": "2020-01-01", "known": "2020-01-02T12:00:00+00:00", "value": .1},
                {"code": "A", "period": "2020-01-01", "known": "2020-01-03T12:00:00+00:00", "value": None}]
        lookup = Adapter.facts(fields, {"roe": rows})
        before = datetime.fromisoformat("2020-01-03T11:59:00+00:00")
        after = datetime.fromisoformat("2020-01-03T12:00:00+00:00")
        self.assertEqual(lookup("roe", "A", date(2020, 1, 3), before), .1)
        self.assertIsNone(lookup("roe", "A", date(2020, 1, 3), after))
        self.assertIsNone(lookup("roe", "B", date(2020, 1, 3), after))

    def test_fact_lookup_rejects_absent_value_column(self):
        from plugins.data import Adapter
        fields = {"roe": {"source": "facts", "code": "code", "effective": "period",
                           "known": "known", "column": "value", "type": "number"}}
        row = {"code": "A", "period": "2020-01-01", "known": "2020-01-02T12:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "columns are invalid"):
            Adapter.facts(fields, {"roe": [row]})

    def test_standard_matches_independent_training_reference(self):
        train = np.array([[1.0, 10.0], [3.0, 14.0], [5.0, 18.0]])
        scaler = Scaler("standard").fit(train)

        expected_center = np.array([3.0, 14.0])
        expected_scale = np.array([np.sqrt(8 / 3), np.sqrt(32 / 3)])
        np.testing.assert_allclose(scaler.center_, expected_center)
        np.testing.assert_allclose(scaler.scale_, expected_scale)
        np.testing.assert_allclose(
            scaler.transform(np.array([[7.0, 22.0]])),
            (np.array([[7.0, 22.0]]) - expected_center) / expected_scale,
        )

    def test_validation_data_does_not_change_training_statistics(self):
        train = np.array([[0.0], [2.0], [4.0]])
        validation = np.array([[100.0]])
        scaler = Scaler("standard").fit(train)

        expected = (validation - 2.0) / np.sqrt(8 / 3)
        np.testing.assert_allclose(scaler.transform(validation), expected)
        np.testing.assert_allclose(scaler.center_, [2.0])
        np.testing.assert_allclose(scaler.scale_, [np.sqrt(8 / 3)])

    def test_minmax_uses_configured_range_without_clipping(self):
        scaler = Scaler("minmax", feature_range=(-2.0, 3.0)).fit([[0.0], [10.0]])

        np.testing.assert_allclose(scaler.transform([[-5.0], [5.0], [15.0]]), [[-4.5], [0.5], [5.5]])

    def test_robust_uses_configured_quantiles(self):
        scaler = Scaler("robust", quantile_range=(20.0, 80.0)).fit([[1.0], [2.0], [3.0], [4.0], [100.0]])

        low = np.quantile([1.0, 2.0, 3.0, 4.0, 100.0], 0.2)
        high = np.quantile([1.0, 2.0, 3.0, 4.0, 100.0], 0.8)
        expected = (np.array([[2.0], [3.0], [4.0]]) - 3.0) / (high - low)
        np.testing.assert_allclose(scaler.transform([[2.0], [3.0], [4.0]]), expected)

    def test_constant_columns_have_scale_one_and_explicit_outputs(self):
        train = [[4.0, 8.0], [4.0, 8.0]]
        validation = [[6.0, 10.0]]

        for method in ("standard", "robust"):
            scaler = Scaler(method).fit(train)
            np.testing.assert_array_equal(scaler.scale_, [1.0, 1.0])
            np.testing.assert_allclose(scaler.transform([[4.0, 8.0]]), [[0.0, 0.0]])
            np.testing.assert_allclose(scaler.transform(validation), [[2.0, 2.0]])

        scaler = Scaler("minmax", feature_range=(2.0, 6.0)).fit(train)
        np.testing.assert_array_equal(scaler.scale_, [1.0, 1.0])
        np.testing.assert_allclose(scaler.transform([[4.0, 8.0]]), [[2.0, 2.0]])
        np.testing.assert_allclose(scaler.transform(validation), [[10.0, 10.0]])

    def test_refit_replaces_all_training_statistics(self):
        scaler = Scaler("standard").fit([[0.0], [2.0]])
        scaler.fit([[10.0], [20.0]])

        np.testing.assert_allclose(scaler.center_, [15.0])
        np.testing.assert_allclose(scaler.scale_, [5.0])
        np.testing.assert_allclose(scaler.transform([[20.0]]), [[1.0]])

    def test_fit_rejects_overflow_without_saving_invalid_statistics(self):
        bad_inputs = {
            "standard": [[1e308], [1e308]],
            "minmax": [[-1e308], [1e308]],
            "robust": [[-1e308], [1e308]],
        }
        for method, values in bad_inputs.items():
            with self.subTest(method=method), self.assertRaises(ValueError):
                Scaler(method).fit(values)

        scaler = Scaler("standard").fit([[0.0], [2.0]])
        old_center = scaler.center_.copy()
        old_scale = scaler.scale_.copy()
        with self.assertRaises(ValueError):
            scaler.fit([[1e308], [1e308]])
        np.testing.assert_array_equal(scaler.center_, old_center)
        np.testing.assert_array_equal(scaler.scale_, old_scale)
        np.testing.assert_allclose(scaler.transform([[2.0]]), [[1.0]])

    def test_failed_refit_preserves_statistics_and_feature_count_for_each_method(self):
        for method in ("standard", "minmax", "robust"):
            with self.subTest(method=method):
                scaler = Scaler(method).fit([[1.0, 10.0], [3.0, 14.0]])
                old_center = scaler.center_.copy()
                old_scale = scaler.scale_.copy()
                old_count = scaler.n_features_in_
                with self.assertRaises(ValueError):
                    scaler.fit([[-1e308, -1e308], [1e308, 1e308]])
                np.testing.assert_array_equal(scaler.center_, old_center)
                np.testing.assert_array_equal(scaler.scale_, old_scale)
                self.assertEqual(scaler.n_features_in_, old_count)

    def test_transform_rejects_overflow_and_nonfinite_output(self):
        scaler = Scaler("standard").fit([[-1e308]])
        with self.assertRaises(ValueError):
            scaler.transform([[1e308]])
        with self.assertRaises(ValueError):
            Scaler("minmax", feature_range=(-1e308, 1e308))

    def test_bool_is_rejected_before_numpy_numeric_coercion(self):
        with self.assertRaises(ValueError):
            Scaler().fit([[True], [2.0]])

    def test_fit_and_transform_reject_invalid_shapes_values_and_parameters(self):
        with self.assertRaisesRegex(ValueError, "not fitted"):
            Scaler().transform([[1.0]])
        for values in ([], [[1.0, 2.0], [3.0]], [[1.0], [np.nan]], [[1.0], [np.inf]], [["1.0"]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Scaler().fit(values)
        scaler = Scaler().fit([[1.0, 2.0]])
        with self.assertRaises(ValueError):
            scaler.transform([[1.0]])
        with self.assertRaises(ValueError):
            scaler.transform([[1.0, np.nan]])
        with self.assertRaises(ValueError):
            Scaler("other")
        with self.assertRaises(ValueError):
            Scaler("minmax", feature_range=(1.0, 1.0))
        with self.assertRaises(ValueError):
            Scaler("robust", quantile_range=(75.0, 25.0))


if __name__ == "__main__":
    unittest.main()
