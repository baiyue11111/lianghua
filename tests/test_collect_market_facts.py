"""Market facts use the normal registered source job."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collect.core import Collect
from collect.methods import DividendHistory, Methods, SinaFactors
from collect.sources import Sources
from market import build_data, fact_ref

SOURCE = {'id': 'stock_adjust', 'name': 'Adjustment', 'method': 'market_facts',
          'frequency': 'daily', 'defaults': {}, 'data_type': 'market_facts',
          'dataset_id': 'cn_a', 'params': {'kind': 'adjust'}}


class MarketFactTests(unittest.TestCase):
    def test_adjustment_partial_response_keeps_old_factors(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            methods = Methods(data)
            rows = [{'date': '2000-01-01', 'forward': 1.0, 'backward': 1.0},
                    {'date': '2025-01-01', 'forward': 2.0, 'backward': 2.0}]
            fact = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600000',
                    'start': '1990-01-01', 'end': '2026-11-12', 'year': None,
                    'source_hash': 'a' * 64, 'records': rows}
            data.save_fact(fact)
            packet = {'records': [{**fact, 'records': [rows[0]], 'raw_page': 0}],
                      'raw': [{}], 'scope': {}, 'request': {}}
            methods.publish(SOURCE, packet)
            result = data.fact(fact_ref('adjust', 'sh.600000', '1990-01-01', '2026-11-12'))
            self.assertEqual(result['records'], rows)

    def test_supersede_replaces_old_adjust_revision_but_keeps_other_identities(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            rows = [{'date': '2000-01-01', 'forward': 1.0, 'backward': 1.0}]
            old = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600000', 'start': '1990-01-01',
                   'end': '2026-11-09', 'year': None, 'source_hash': 'a' * 64, 'records': rows}
            new = {**old, 'end': '2026-11-12', 'source_hash': 'b' * 64,
                   'records': rows + [{'date': '2006-05-12', 'forward': 1.2, 'backward': 2.0}]}
            dividend = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                        'end': None, 'year': '2025', 'source_hash': 'c' * 64, 'records': []}
            data.save_facts([old, dividend])
            old_ref = fact_ref('adjust', 'sh.600000', '1990-01-01', '2026-11-09')
            dividend_ref = fact_ref('dividend', 'sh.600000', year='2025')
            self.assertIn(old_ref, data.facts((old_ref,)))

            data.save_facts([new], supersede=True)

            new_ref = fact_ref('adjust', 'sh.600000', '1990-01-01', '2026-11-12')
            self.assertNotIn(old_ref, data.facts((old_ref,)))
            self.assertEqual(len(data.fact(new_ref)['records']), 2)
            self.assertIn(dividend_ref, data.facts((dividend_ref,)))

    def test_adjustment_merge_requires_common_factor_anchor(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            methods = Methods(data)
            old = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600000',
                   'start': '1990-01-01', 'end': '2026-11-12', 'year': None,
                   'source_hash': 'a' * 64,
                   'records': [{'date': '2000-01-01', 'forward': 1.0, 'backward': 1.0}]}
            data.save_fact(old)
            incoming = {**old, 'source_hash': 'b' * 64,
                        'records': [{'date': '2020-01-01', 'forward': 2.0, 'backward': 2.0}]}
            packet = {'records': [{**incoming, 'raw_page': 0}], 'raw': [{}], 'scope': {}, 'request': {}}
            result = methods.publish(SOURCE, packet)
            self.assertEqual(result['records'], 0)
            self.assertEqual(len(result['failures']), 1)
            ref = fact_ref('adjust', 'sh.600000', '1990-01-01', '2026-11-12')
            self.assertEqual(data.fact(ref), old)
            packet['records'][0]['records'] = old['records'] + incoming['records']
            result = methods.publish(SOURCE, packet)
            self.assertEqual(result['records'], 1)
            self.assertEqual(len(data.fact(ref)['records']), 2)
    def test_security_refresh_does_not_erase_delisting(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            methods = Methods(data)
            source = {**SOURCE, 'id': 'stock_security', 'params': {'kind': 'security'}}
            fact = {'schema': 1, 'kind': 'security', 'code': 'sh.600000', 'start': None,
                    'end': None, 'year': None, 'source_hash': 'a' * 64, 'records': [
                        {'listing_date': '1999-01-01', 'delisting_date': '2024-01-01', 'type': '1', 'status': '0'}]}
            data.save_fact(fact)
            incoming = {**fact, 'source_hash': 'b' * 64, 'records': [
                {'listing_date': '1999-01-01', 'delisting_date': None, 'type': '1', 'status': '1'}]}
            result = methods.publish(source, {'records': [{**incoming, 'raw_page': 0}],
                                              'raw': [{}], 'scope': {}, 'request': {}})
            self.assertEqual(result['records'], 0)
            self.assertEqual(len(result['failures']), 1)
            self.assertEqual(data.fact(fact_ref('security', 'sh.600000')), fact)

    def test_concurrent_dividend_updates_keep_both_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            source = {**SOURCE, 'id': 'stock_dividend', 'params': {'kind': 'dividend'}}
            methods = Methods(data)
            fact = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                    'end': None, 'year': '2025', 'source_hash': 'a' * 64, 'records': []}
            data.save_fact(fact)
            first_read, second_read = threading.Event(), threading.Event()
            guard = threading.Lock()
            calls = []
            read = data.facts
            def synchronized_read(*args, **kwargs):
                result = read(*args, **kwargs)
                with guard:
                    calls.append(1)
                    first = len(calls) == 1
                if first:
                    first_read.set()
                    second_read.wait(.5)
                else:
                    second_read.set()
                return result
            def packet(day):
                row = {'announce_date': f'2025-0{day}-01', 'registration_date': f'2025-0{day}-02',
                       'operation_date': f'2025-0{day}-03', 'payment_date': f'2025-0{day}-04',
                       'stock_market_date': None, 'cash_per_share': .1,
                       'stock_dividend_per_share': 0.0, 'reserve_to_stock_per_share': 0.0,
                       'cash_after_tax_text': None}
                return {'records': [{**fact, 'records': [row], 'raw_page': 0}],
                        'raw': [{}], 'scope': {}, 'request': {}}
            errors = []
            def write(day):
                try:
                    methods._deliver(source, packet(day), dry_run=False)
                except Exception as error:
                    errors.append(error)
            with patch.object(data, 'facts', side_effect=synchronized_read):
                first = threading.Thread(target=write, args=(1,))
                second = threading.Thread(target=write, args=(2,))
                first.start()
                self.assertTrue(first_read.wait(2))
                second.start()
                first.join(5)
                second.join(5)
                self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            result = data.fact(fact_ref('dividend', 'sh.600000', year='2025'))
            self.assertEqual([row['operation_date'] for row in result['records']],
                             ['2025-01-03', '2025-02-03'])

    def test_fact_coverage_checks_each_security_and_does_not_infer_full_year(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            methods = Methods(data)
            good = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600000',
                    'start': '1990-01-01', 'end': '2026-11-12', 'year': None,
                    'source_hash': 'a' * 64,
                    'records': [{'date': '1999-01-01', 'forward': 1.0, 'backward': 1.0}]}
            data.save_fact(good)
            request = {'codes': ['sh.600000', 'sh.600001'], 'start': '2026-11-10', 'end': '2026-11-12'}
            coverage = methods.coverage(SOURCE, request)
            self.assertFalse(coverage['complete'])
            self.assertEqual(coverage['missing'], ['sh.600001'])
            source = {**SOURCE, 'params': {'kind': 'dividend'}}
            dividend = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                        'end': None, 'year': '2026', 'source_hash': 'b' * 64, 'records': []}
            data.save_fact(dividend)
            coverage = methods.coverage(source, {**request, 'codes': ['sh.600000']})
            self.assertFalse(coverage['complete'])
            self.assertEqual(coverage['unknown'], ['sh.600000'])

    def test_dividend_partial_response_preserves_existing_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            source = {**SOURCE, 'id': 'stock_dividend', 'params': {'kind': 'dividend'}}
            methods = Methods(data)
            row = {'announce_date': '2025-03-01', 'registration_date': '2025-05-01',
                   'operation_date': '2025-05-02', 'payment_date': '2025-05-02',
                   'stock_market_date': None, 'cash_per_share': 0.1,
                   'stock_dividend_per_share': 0.0, 'reserve_to_stock_per_share': 0.0,
                   'cash_after_tax_text': None}
            old = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                   'end': None, 'year': '2025', 'source_hash': 'a' * 64, 'records': [row]}
            data.save_fact(old)
            packet = {'records': [{**old, 'source_hash': 'b' * 64, 'records': [], 'raw_page': 0}],
                      'raw': [{'response': 'partial'}], 'scope': {}, 'request': {}}
            methods.publish(source, packet)
            actual = data.fact(fact_ref('dividend', 'sh.600000', year='2025'))
            self.assertEqual(actual['records'], [row])
            conflicting = {**row, 'cash_per_share': 0.2}
            later = {**old, 'year': '2026', 'records': []}
            packet['records'] = [{**old, 'records': [conflicting], 'raw_page': 0},
                                 {**later, 'raw_page': 0}]
            result = methods.publish(source, packet)
            self.assertEqual(result['records'], 1)
            self.assertEqual(result['failures'][0]['year'], '2025')
            self.assertEqual(data.fact(fact_ref('dividend', 'sh.600000', year='2025'))['records'], [row])
            self.assertEqual(data.fact(fact_ref('dividend', 'sh.600000', year='2026'))['records'], [])

    def test_merged_dividend_retains_both_source_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            source = {**SOURCE, 'id': 'stock_dividend', 'params': {'kind': 'dividend'}}
            methods = Methods(data)
            row = {'announce_date': '2025-03-01', 'registration_date': '2025-05-01',
                   'operation_date': '2025-05-02', 'payment_date': '2025-05-02',
                   'stock_market_date': None, 'cash_per_share': 0.1,
                   'stock_dividend_per_share': 0.0, 'reserve_to_stock_per_share': 0.0,
                   'cash_after_tax_text': None}
            fact = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                    'end': None, 'year': '2025', 'source_hash': 'a' * 64, 'records': [row]}
            methods.publish(source, {'records': [{**fact, 'raw_page': 0}],
                                     'raw': [{'source': 'first'}], 'scope': {}, 'request': {}})
            later = {**row, 'announce_date': '2025-08-01', 'registration_date': '2025-09-01',
                     'operation_date': '2025-09-02', 'payment_date': '2025-09-02'}
            methods.publish(source, {'records': [{**fact, 'source_hash': 'b' * 64,
                                                  'records': [later], 'raw_page': 0}],
                                     'raw': [{'source': 'second'}], 'scope': {}, 'request': {}})
            actual = data.fact(fact_ref('dividend', 'sh.600000', year='2025'))
            self.assertEqual(len(actual['records']), 2)
            self.assertEqual(methods.record_evidence(actual), [{'source': 'second'}, {'source': 'first'}])

    def test_merged_fact_reports_missing_historical_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            self.addCleanup(data.close)
            methods = Methods(data)
            source = {**SOURCE, 'id': 'stock_dividend', 'params': {'kind': 'dividend'}}
            row = {'announce_date': '2025-03-01', 'registration_date': '2025-05-01',
                   'operation_date': '2025-05-02', 'payment_date': '2025-05-02',
                   'stock_market_date': None, 'cash_per_share': .1, 'stock_dividend_per_share': .0,
                   'reserve_to_stock_per_share': .0, 'cash_after_tax_text': None}
            fact = {'schema': 1, 'kind': 'dividend', 'code': 'sh.600000', 'start': None,
                    'end': None, 'year': '2025', 'source_hash': 'a' * 64, 'records': [row]}
            data.save_fact(fact)
            later = {**row, 'registration_date': '2025-09-01', 'operation_date': '2025-09-02'}
            methods.publish(source, {'records': [{**fact, 'source_hash': 'b' * 64,
                                                  'records': [later], 'raw_page': 0}],
                                     'raw': [{'source': 'second'}], 'scope': {}, 'request': {}})
            actual = data.fact(fact_ref('dividend', 'sh.600000', year='2025'))
            evidence = methods.record_evidence(actual)
            self.assertEqual(evidence[0], {'source': 'second'})
            self.assertEqual(evidence[1]['status'], 'missing')
            self.assertIn('record_hash', evidence[1])

    def test_changes_use_registered_catalogue_and_preserve_notice_evidence(self):
        from collect.methods import Catalogue, CninfoAnnouncements
        source = {**SOURCE, 'id': 'security_changes', 'method': 'code_changes',
                  'data_type': 'code_changes', 'params': {'key_fields': [
                      'exchange', 'effective_date', 'old_code', 'new_code']}}
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            try:
                rows = [{'code': 'sz.000111', 'ipoDate': '2000-01-01', 'outDate': '2026-11-12'},
                        {'code': 'sz.000420', 'ipoDate': '2000-01-01', 'outDate': ''}]
                data.put_rows('test_catalogue', rows, keys=('code',))
                data.bind('catalogue', 'records', 'lake:test_catalogue')
                methods = Methods(data)
                registry = {'markets': [{'dataset_id': 'cn_a', 'code_pattern': r'sz\.\d{6}',
                    'collect': {'securities': {'source': 'catalogue'}}}]}
                manifest = {'inputs': {}}
                announcement = {'records': [{'code': '000111', 'title': '证券代码变更实施公告',
                    'url': 'https://static.cninfo.com.cn/finalpage/2026-11-11/notice.pdf'},
                    {'code': '000999', 'title': '证券代码变更实施公告',
                     'url': 'https://static.cninfo.com.cn/finalpage/2026-11-11/unknown.pdf'}], 'raw': []}
                event = {'exchange': 'SZ', 'old_code': '000111', 'new_code': '000420',
                    'effective_date': '2026-11-12', 'announcement_date': '2026-11-11',
                    'source_url': announcement['records'][0]['url'], 'holder_share_ratio': 1,
                    'holding_period_continues': True, 'factor_basis_continuity_verified': False}
                response = SimpleNamespace(content=b'%PDF-fixture', raise_for_status=lambda: None)
                pdf = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: 'notice text')])
                with patch.object(data, 'registry', return_value=registry), \
                     patch.object(data, 'manifest', return_value=manifest), \
                     patch.object(CninfoAnnouncements, 'collect', return_value=announcement) as query, \
                     patch.object(methods._http, 'get', return_value=response), \
                     patch('pypdf.PdfReader', return_value=pdf), \
                     patch.object(Catalogue, 'rename', return_value=[event]):
                    packet = methods.fetch(source, {'start': '2026-11-10', 'end': '2026-11-12'})
                self.assertEqual(query.call_count, 1)
                self.assertEqual(packet['records'][0]['new_code'], '000420')
                self.assertTrue(packet['records'][0]['evidence_sha256'])
                self.assertTrue(packet['raw'][1]['pdf'])
                self.assertEqual(packet['failures'][0]['code'], '000999')
                methods.publish(source, packet)
                self.assertEqual(methods.records(source)['records'][0]['old_code'], '000111')
            finally:
                data.close()

    def test_dividend_history_fetches_page_once_and_isolates_years(self):
        calls = []
        html = '<title>600000</title><table id="sharebonus_1"><tbody><tr><td>x</td></tr></tbody></table>'
        response = SimpleNamespace(text=html, raise_for_status=lambda: None, encoding=None)
        def get(url, **kwargs):
            calls.append(url)
            return response
        def normalize(base, *args, **kwargs):
            if base['year'] == '2025':
                raise ValueError('ambiguous notice')
            return base
        with patch.object(DividendHistory, 'parse', return_value=('parsed', 'hash')), \
             patch.object(DividendHistory, 'normalize', side_effect=normalize):
            raw, facts, failures = DividendHistory.collect('sh.600000', ['2025', '2026'],
                                                             end='2026-11-12', get=get)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(facts), ['2026'])
        self.assertEqual(failures[0]['year'], '2025')
        self.assertEqual(raw['html'], html)

    def test_security_source_publishes_catalogue_fact(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            try:
                source = {**SOURCE, 'id': 'stock_security', 'params': {'kind': 'security'}}
                sources = Sources(data)
                sources.register(source)
                methods = Methods(data)
                engine = Collect(sources, methods)
                try:
                    registry = data.registry()
                    registry['markets'] = [{'dataset_id': 'cn_a', 'code_pattern': r'sh\.\d{6}',
                        'collect': {'securities': {'source': 'catalogue'}}}]
                    row = {'code': 'sh.600000', 'ipoDate': '1999-01-01', 'outDate': '',
                           'type': '1', 'status': '1'}
                    data.put_rows('test_catalogue', [row], keys=('code',))
                    data.bind('catalogue', 'records', 'lake:test_catalogue')
                    with patch.object(data, 'registry', return_value=registry):
                        job = engine.run({'source_id': source['id'], 'codes': ['sh.600000'],
                                          'start': '2026-11-10', 'end': '2026-11-12'})
                    self.assertEqual((job['outcome'], job['records']), ('succeeded', 1))
                    self.assertEqual(data.fact(fact_ref('security', 'sh.600000'))['records'][0]['listing_date'], '1999-01-01')
                finally:
                    engine.close()
            finally:
                data.close()

    def test_dividend_failure_does_not_discard_another_year(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            try:
                source = {**SOURCE, 'id': 'stock_dividend', 'params': {'kind': 'dividend'}}
                sources = Sources(data)
                sources.register(source)
                engine = Collect(sources, Methods(data))
                try:
                    def collect(code, years, **kwargs):
                        return {'years': years}, {'2026': {'schema': 1, 'kind': 'dividend',
                            'code': code, 'start': None, 'end': None, 'year': '2026',
                            'source_hash': 'a' * 64, 'records': []}}, [
                            {'code': code, 'year': '2025', 'error': 'provider failed'}]
                    with patch.object(DividendHistory, 'collect', side_effect=collect) as fetch:
                        job = engine.run({'source_id': source['id'], 'codes': ['sh.600000'],
                                          'start': '2025-01-01', 'end': '2026-11-12'})
                    self.assertEqual(fetch.call_count, 1)
                    self.assertEqual(fetch.call_args.args[1], ['2025', '2026'])
                    self.assertEqual((job['outcome'], job['records']), ('partial', 1))
                    self.assertEqual(job['failures'][0]['year'], '2025')
                    self.assertEqual(data.fact(fact_ref('dividend', 'sh.600000', year='2026'))['records'], [])
                finally:
                    engine.close()
            finally:
                data.close()

    def test_adjustment_is_published_as_a_normal_collection_job(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data(Path(folder))
            try:
                code = 'sh.600000'
                security = {'schema': 1, 'kind': 'security', 'code': code, 'start': None,
                    'end': None, 'year': None, 'source_hash': 'a' * 64,
                    'records': [{'listing_date': '1999-01-01', 'delisting_date': None,
                                 'type': '1', 'status': '1'}]}
                data.save_fact(security)
                result = {'schema': 1, 'kind': 'adjust', 'code': code, 'start': '1990-01-01',
                    'end': '2026-11-12', 'year': None, 'source_hash': 'b' * 64,
                    'records': [{'date': '1999-01-01', 'forward': 1.0, 'backward': 1.0}]}
                sources = Sources(data)
                sources.register(SOURCE)
                engine = Collect(sources, Methods(data))
                try:
                    manifest = {'inputs': {'adjustments': {'start': '1990-01-01'}}}
                    with patch.object(data, 'manifest', return_value=manifest), \
                         patch.object(SinaFactors, 'collect', return_value=({'provider': 'test'}, result)):
                        job = engine.run({'source_id': SOURCE['id'], 'codes': [code],
                                          'start': '2026-11-10', 'end': '2026-11-12'})
                    self.assertEqual((job['status'], job['outcome'], job['records']), ('finished', 'succeeded', 1))
                    self.assertEqual(data.fact(fact_ref('adjust', code, '1990-01-01', '2026-11-12')), result)
                    self.assertEqual(engine.methods.last_dates(SOURCE, [code]), {code: '2026-11-12'})
                    snapshot = engine.methods.records(SOURCE, 0, 0)
                    self.assertEqual(snapshot['total'], 1)
                    self.assertTrue(snapshot['snapshot_id'])
                    page = engine.methods.records(SOURCE, 0, 1)
                    self.assertEqual(page['records'], [result])
                    self.assertEqual(page['raw'], [{'provider': 'test'}])
                finally:
                    engine.close()
            finally:
                data.close()


if __name__ == '__main__':
    unittest.main()
