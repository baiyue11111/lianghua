import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from market import build_data
from protocol import DataError, DataQuery
from collect import methods as factors


class FactorStorageTests(unittest.TestCase):
    def test_daily_refresh_replaces_only_requested_code_and_interval(self):
        from tests.history_fixture import make_snapshot
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            make_snapshot(root,symbols=('600003','600004'))
            bus=build_data(root)
            methods=factors.Methods(bus)
            source=dict(id='prices',method='daily',dataset_id='fixture',storage_id='fixture')
            rows=[dict(code=code,date=day,open=12.,high=12.,low=12.,close=12.,volume=100.,amount=1200.)
                  for code in ('sh.600003','sh.600004') for day in ('2019-12-31','2020-02-06','2020-02-07')]
            packet=dict(records=rows,raw=[],scope={},request=dict(codes=['sh.600003','sh.600004'],
                start='2019-12-31',end='2020-02-07'),calendar=dict(sessions=['2019-12-31','2020-02-06','2020-02-07'],
                coverage=[dict(start='2019-12-31',end='2020-02-07')]))
            methods.publish(source,packet)
            packet['records']=[rows[1]]
            packet['request'].update(codes=['sh.600003'],refresh=True)
            methods.publish(source,packet)
            actual=pq.read_table(pa.BufferReader(bus.read('sources','fixture','daily/2020.parquet'))).to_pylist()
            self.assertEqual({(r['code'],r['date']) for r in actual},
                {('sh.600003','2020-02-06'),('sh.600004','2020-02-06'),('sh.600004','2020-02-07')})
            previous=pq.read_table(pa.BufferReader(bus.read('sources','fixture','daily/2019.parquet'))).to_pylist()
            self.assertEqual({(r['code'],r['date']) for r in previous},{('sh.600004','2019-12-31')})

    def test_daily_publish_is_independent_of_missing_distribution_facts(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            make_snapshot(root, symbols=('600003',))
            bus=build_data(root)
            manifest=bus.manifest('fixture')
            bus.delete('sources','fixture','corp/dividend_sh.600003_2019.json')
            manifest['files']=[r for r in manifest['files'] if r['file']!='corp/dividend_sh.600003_2019.json']
            bus.write('sources','fixture','manifest.json',json.dumps(manifest).encode())
            packet=dict(records=[dict(code='sh.600003',date='2020-02-06',open=12.,high=12.,
                low=12.,close=12.,volume=100.,amount=1200.)],raw=[],scope={},
                request=dict(codes=['sh.600003'],start='2020-02-06',end='2020-02-06'),
                calendar=dict(sessions=['2020-02-06','2020-02-07'],
                    coverage=[dict(start='2020-02-06',end='2020-02-07')]))
            methods=factors.Methods(bus)
            with patch.object(methods,'complete_facts',side_effect=AssertionError('unrelated fact collection')):
                methods.publish(dict(id='prices',method='daily',dataset_id='fixture',storage_id='fixture'),packet)
            self.assertEqual(bus.manifest('fixture')['inputs']['adjustments']['end'],'2020-02-05')
            self.assertTrue(methods.daily_coverage('fixture',('sh.600003',),date(2020,2,6),
                date(2020,2,6),('close',))['complete'])
            self.assertFalse(methods.complete_facts('fixture',bus.manifest('fixture'),packet['request'],check=True))

    def test_fact_failure_does_not_publish_daily_or_leave_a_partial_transaction(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003', '600004'))
            bus = build_data(root)
            manifest = bus.read('sources', 'fixture', 'manifest.json')
            first = {'schema': 1, 'kind': 'adjust', 'code': 'sh.600003',
                     'start': '1990-01-01', 'end': '2020-02-06', 'year': None,
                     'source_hash': 'a'*64, 'records': [{'date': '1990-01-01', 'forward': 1., 'backward': 1.}]}
            def dividends(code, years, *, end, **kwargs):
                return {}, {year:dict(schema=1,kind='dividend',code=code,start=None,end=None,
                                     year=year,source_hash='a'*64,records=[]) for year in years}
            with patch.object(factors.SinaFactors, 'collect', side_effect=[({},first), factors.ProviderError('factor unavailable')]), \
                 patch.object(factors.DividendHistory, 'collect', side_effect=dividends):
                with self.assertRaisesRegex(factors.CollectionError, 'sh.600004 adjust.*factor unavailable'):
                    with bus.transaction('sources'):
                        factors.Methods(bus).complete_facts('fixture', bus.manifest('fixture'),
                            {'codes': ['sh.600003'], 'start': '2020-02-04', 'end': '2020-02-06'})
            self.assertEqual(bus.read('sources', 'fixture', 'manifest.json'), manifest)
            self.assertFalse(list((root/'data/source/fixture/factors').glob('*2020-02-06.json')))

    def test_missing_fact_does_not_refetch_complete_daily_prices(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            source = dict(id='prices', method='daily', dataset_id='fixture')
            request = dict(codes=['sh.600003'], start='2020-01-31', end='2020-02-04')
            methods = factors.Methods(bus)
            with patch.object(methods, 'daily_coverage', return_value={'complete': True}):
                self.assertTrue(methods.coverage(source, request)['complete'])
                path = root/'data/source/fixture/manifest.json'
                value = json.loads(path.read_text())
                name = 'corp/dividend_sh.600003_2019.json'
                (root/'data/source/fixture'/name).unlink()
                value['files'] = [r for r in value['files'] if r['file'] != name]
                path.write_text(json.dumps(value))
                self.assertTrue(methods.coverage(source, request)['complete'])

    def test_latest_refreshes_the_current_year_but_preserves_older_dividends(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            manifest = bus.manifest('fixture')
            collected = []
            def fetch(request, provider):
                collected.append(request)
                name = (f'factors/adjust_{request.code}_{request.start}_{request.end}.json'
                        if request.kind == 'adjust' else f'corp/dividend_{request.code}_{request.year}.json')
                return json.loads(bus.read('sources', 'fixture', name))
            row = {'code':'sh.600003','ipoDate':'2019-07-01','outDate':'','type':'1','status':'1'}
            bus.write('sources','catalogue','records.json',json.dumps({'records':[row]}).encode())
            registry = json.loads(bus.read('sources','registry','sources.json'))
            registry['markets'][0]['collect'] = {'securities':{'source':'catalogue'}}
            bus.write('sources','registry','sources.json',json.dumps(registry).encode())
            with patch.object(factors.SinaFactors, 'collect', side_effect=lambda task, security, **kwargs: ({},fetch(task,None))), \
                 patch.object(factors.DividendHistory, 'collect', side_effect=lambda code, years, end, **kwargs: ({}, {year:fetch(factors.Request('dividend',code,None,None,year),None) for year in years})):
                with bus.transaction('sources'):
                    factors.Methods(bus).complete_facts('fixture', manifest, {
                        'codes': ['sh.600003'], 'start': '2020-02-04', 'end': '2020-02-05', 'refresh': True})
            self.assertEqual([r.year for r in collected if r.kind == 'dividend'], ['2020'])

    def test_doctor_repairs_missing_and_invalid_facts_without_fetching_bars(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            methods = factors.Methods(bus)
            source = dict(id='prices', method='daily', dataset_id='fixture')
            manifest = bus.manifest('fixture')
            name = manifest['inputs']['adjustments']['template'].format(
                symbol='sh.600003', start=manifest['inputs']['adjustments']['start'],
                end=manifest['inputs']['adjustments']['end'])
            path = root/'data/source/fixture'/name
            original = path.read_bytes()
            fact = json.loads(original)
            for broken in (None, b'{broken'):
                if broken is None:
                    path.unlink()
                else:
                    path.write_bytes(broken)
                with patch.object(factors.SinaFactors, 'collect', return_value=({},fact)) as fetch, \
                     patch.object(methods, 'fetch', side_effect=AssertionError('daily fetch is forbidden')):
                    result = methods.doctor(source, {'codes': ['sh.600003']})
                    self.assertEqual(result['status'], 'complete')
                    self.assertEqual(fetch.call_count, 1)
                self.assertEqual(json.loads(path.read_bytes()), fact)
                with patch.object(factors.SinaFactors, 'collect', side_effect=AssertionError('repeat fetch')):
                    self.assertEqual(methods.doctor(source, {'codes': ['sh.600003']})['status'], 'complete')

    def test_evidence_is_written_and_verified_through_protocol(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        response = SimpleNamespace(error_code='0', error_msg='', code='sh.600000',
                                   fields=['code', 'dividOperateDate', 'foreAdjustFactor', 'backAdjustFactor'],
                                   data=[['sh.600000', '2020-01-01', '1.2', '1.1']])
        response.next = Mock(side_effect=[True, False])
        response.get_row_data = Mock(return_value=response.data[0])
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            ref = factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual((ref.identity, ref.name), ('facts', f'{request.key}.json'))
            self.assertEqual(factors.pending_requests([request], bus), [])
            payload = json.loads(bus.read(ref.kind, ref.identity, ref.name))
            raw_log = json.loads(bus.read('state', 'collect', bus.list('state', 'collect')[0].name))
            self.assertEqual(payload['source_hash'], raw_log['raw_response_sha256'])
            payload['records'][0]['forward'] = -9
            bus.write(ref.kind, ref.identity, ref.name, json.dumps(payload).encode())
            with self.assertRaises(factors.CacheIntegrityError):
                factors.pending_requests([request], bus)

    def test_failed_provider_creates_no_cache(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=SimpleNamespace(error_code='10002007', error_msg='failed')))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            with self.assertRaises(factors.ProviderError):
                factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual(bus.list('sources', 'facts'), ())
            provider.query_adjust_factor.assert_called_once()


class SinaFactorTests(unittest.TestCase):
    def setUp(self):
        self.request = factors.Request('adjust', 'sz.000420', '1990-01-01', '2026-10-13', None)
        self.security = {'schema':1, 'kind':'security', 'code':'sz.000420', 'start':None,
            'end':None, 'year':None, 'source_hash':'a'*64, 'records':[{'listing_date':'1999-01-01',
            'delisting_date':None, 'type':'1', 'status':'1'}]}
        self.rows = {'qfq':[('2020-01-01','1'),('2014-01-01','2'),('1999-01-01','4')],
                     'hfq':[('2020-01-01','4'),('2014-01-01','2'),('1999-01-01','1')]}

    def payloads(self):
        return {kind:'var sz000420'+kind+'='+json.dumps({'total':len(rows),
            'data':[{'d':day,'f':value} for day,value in rows]}) for kind,rows in self.rows.items()}

    def test_pair_becomes_standard_facts_and_respects_security_end(self):
        result = factors.SinaFactors.normalize(self.request, self.security, self.payloads())
        self.assertEqual(result['records'][1], {'date':'2014-01-01','forward':.5,'backward':2.})
        self.security['records'][0].update(delisting_date='2015-01-01',status='0')
        result = factors.SinaFactors.normalize(self.request, self.security, self.payloads())
        self.assertEqual(result['records'][-1], {'date':'2014-01-01','forward':1.,'backward':2.})
        factors._validate_cache(self.request, result)

    def test_exact_cash_dividend_is_not_rounded_before_adjustment(self):
        expected = (11.6 - .249) / 11.6
        inverse = str(1 / expected)
        self.rows = {'qfq':[('2026-09-24','1'),('1999-01-01',inverse)],
                     'hfq':[('2026-09-24',inverse),('1999-01-01','1')]}
        result = factors.SinaFactors.normalize(self.request, self.security, self.payloads())
        self.assertAlmostEqual(result['records'][0]['forward'], expected, places=14)
        self.assertNotAlmostEqual(result['records'][0]['forward'], (11.6 - .25) / 11.6, places=6)

    def test_malformed_identity_count_order_and_pair_are_rejected(self):
        from copy import deepcopy
        valid = self.payloads()
        variants = [dict(valid,qfq=valid['qfq'].replace('sz000420qfq','sh600000qfq')),
                    dict(valid,qfq=valid['qfq'].replace('"total": 3','"total": 4'))]
        for kind, rows in [('qfq',[('2020-01-01','0')]),
                           ('qfq',[('2020-01-01','1'),('2020-01-01','1')]),
                           ('hfq',[('2020-01-01','9'),('2014-01-01','2'),('1999-01-01','1')])]:
            old=deepcopy(self.rows);self.rows[kind]=rows;variants.append(self.payloads());self.rows=old
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises((ValueError, factors.CacheIntegrityError)):
                factors.SinaFactors.normalize(self.request, self.security, raw)
        self.security['records'][0]['listing_date']=None
        with self.assertRaises(ValueError):
            factors.SinaFactors.normalize(self.request, self.security, valid)

class IndependentFactTests(unittest.TestCase):
    def test_registered_catalogue_row_becomes_security_fact(self):
        row = {'code':'sz.000043','ipoDate':'1994-09-28','outDate':'2019-12-16','type':'1','status':'0'}
        fact = factors.Catalogue.security('sz.000043', row)
        self.assertEqual(fact['records'][0]['listing_date'], '1994-09-28')
        self.assertEqual(fact['records'][0]['delisting_date'], '2019-12-16')
        with self.assertRaises(ValueError):
            factors.Catalogue.security('sz.001914', row)
        with self.assertRaises(ValueError):
            factors.Catalogue.security('sz.000043', {**row,'outDate':''})

    def test_factor_collection_returns_both_raw_responses_and_standard_facts(self):
        fixture = SinaFactorTests(); fixture.setUp()
        payloads = fixture.payloads()
        def response(url, **kwargs):
            kind = url.rsplit('/',1)[1].split('.')[0]
            return SimpleNamespace(raise_for_status=lambda:None, text=payloads[kind])
        with patch('requests.get', side_effect=response):
            raw, fact = factors.SinaFactors.collect(fixture.request, fixture.security)
        self.assertEqual(raw['responses'], payloads)
        self.assertEqual(raw['security'], fixture.security)
        self.assertEqual(fact, factors.SinaFactors.normalize(fixture.request, fixture.security, payloads))

class IndependentDividendTests(unittest.TestCase):
    def test_complete_public_table_proves_an_empty_year_and_rejects_wrong_page(self):
        html = ('<title>000420分红配股</title><table id="sharebonus_1"><thead><tr>'
                '<th colspan="8">分红方案(每10股)</th></tr><tr><th>公告日期</th><th>送股(股)</th>'
                '<th>转增(股)</th><th>派息(税前)(元)</th><th>进度</th><th>除权除息日</th>'
                '<th>股权登记日</th><th>红股上市日</th></tr></thead><tbody><tr><td>2020-04-01</td>'
                '<td>0</td><td>0</td><td>0</td><td>不分配</td><td>--</td><td>--</td><td>--</td>'
                '</tr></tbody></table>')
        def response(text): return SimpleNamespace(raise_for_status=lambda:None, text=text)
        with patch('requests.get', return_value=response(html)):
            raw, facts = factors.DividendHistory.collect('sz.000420', ['2019','2020'], end='2020-12-31')
        self.assertEqual(set(facts), {'2019','2020'})
        self.assertTrue(all(f['records']==[] for f in facts.values()))
        self.assertEqual(raw['html'], html)
        for broken in (html.replace('000420','000421'), html.replace('sharebonus_1','unknown'),
                       html+'<a href="?page=2">下一页</a>'):
            with patch('requests.get', return_value=response(broken)), self.assertRaises(ValueError):
                factors.DividendHistory.collect('sz.000420', ['2020'], end='2020-12-31')

class DividendNoticeTests(unittest.TestCase):
    def text(self, code='000123'):
        return (f'证券代码：{code} 公告日期：2008年6月30日 '
                '资本公积每10股转增1.234567股 '
                '资本公积转增股本股权登记日：2008年7月2日 '
                '资本公积转增股本除权日：2008年7月3日 '
                '新增可流通股份上市日：2008年7月3日 本次转增股份将于2008年7月3日直接记入股东证券帐户 '
                '资本公积转增股本后，按照派送现金红利股权登记日的总股本为基数，每10股派0.123456元（含税）； '
                '派送现金红利股权登记日：2008年7月4日 '
                '除息及现金红利发放日：2008年7月7日')

    def test_components_keep_their_own_dates_and_exact_amounts(self):
        rows = factors.DividendHistory.notice('sz.000123', '2008-06-30', self.text())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['operation_date'], '2008-07-03')
        self.assertEqual(rows[0]['reserve_to_stock_per_share'], .1234567)
        self.assertEqual(rows[0]['cash_per_share'], 0)
        self.assertEqual(rows[1]['registration_date'], '2008-07-04')
        self.assertEqual(rows[1]['payment_date'], '2008-07-07')
        self.assertEqual(rows[1]['cash_per_share'], .0123456)
        self.assertEqual(rows[1]['reserve_to_stock_per_share'], 0)

    def test_missing_conflicting_or_wrong_identity_evidence_is_rejected(self):
        for text in (self.text('000124'), self.text().replace('新增可流通股份上市日','其他日期'),
                     self.text().replace('直接记入股东证券帐户','未给出到账日期'),
                     self.text()+'资本公积每10股转增2股',
                     self.text().replace('2008年7月7日','2008年7月1日')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                factors.DividendHistory.notice('sz.000123','2008-06-30',text)

class SplitDividendTests(unittest.TestCase):
    def test_notice_expands_history_row_and_filters_each_event_at_end(self):
        fixture = DividendNoticeTests()
        text = fixture.text()
        html = ('<table><thead><tr><th colspan="8">分红方案(每10股)</th></tr><tr>'
                '<th>公告日期</th><th>送股(股)</th><th>转增(股)</th><th>派息(税前)(元)</th>'
                '<th>进度</th><th>除权除息日</th><th>股权登记日</th><th>红股上市日</th>'
                '</tr></thead><tbody><tr><td>2008-06-30</td><td>0</td><td>1.23457</td>'
                '<td>0.123456</td><td>实施</td><td>2008-07-07</td><td>2008-07-04</td><td>--</td></tr></tbody></table>')
        detail = ('<title>000123分红明细</title><table><tr><td>登记日</td><td>2008-07-02</td></tr>'
                  '<tr><td>除息日</td><td>2008-07-03</td></tr></table>')
        base = dict(schema=1,kind='dividend',code='sz.000123',start=None,end=None,year='2008',source_hash='a'*64,records=[])
        def resolve(notice, registered): return {'records':factors.DividendHistory.notice(base['code'],notice,text),'source':text}
        result = factors.DividendHistory.normalize(base,html,load=lambda url:detail,resolve=resolve)
        self.assertEqual([r['operation_date'] for r in result['records']], ['2008-07-03','2008-07-07'])
        early = factors.DividendHistory.normalize(base,html,end='2008-07-04',load=lambda url:detail,resolve=resolve)
        self.assertEqual([r['operation_date'] for r in early['records']], ['2008-07-03'])
        with self.assertRaises(ValueError):
            factors.DividendHistory.normalize(base,html.replace('1.23457','2.23457'),load=lambda url:detail,resolve=resolve)
        with self.assertRaises(ValueError):
            factors.DividendHistory.normalize(base,html,load=lambda url:detail)

class CapitalNoticeTests(unittest.TestCase):
    def test_single_capital_action_uses_explicit_registration_and_credit(self):
        text = ('证券代码：000321 转增股本实施公告 每10股转增6股。'
                '股权登记日：2009年7月2日 除权日：2009年7月3日 '
                '新增可流通股份上市日：2009年7月3日 股份到帐日为2009年7月3日；')
        rows = factors.DividendHistory.notice('sz.000321','2009-06-30',text)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['reserve_to_stock_per_share'], .6)
        self.assertEqual(rows[0]['cash_per_share'],0)
        for broken in (text.replace('股份到帐日为','到账未明确'),text+'每10股派0.1元（含税）'):
            with self.assertRaises(ValueError):
                factors.DividendHistory.notice('sz.000321','2009-06-30',broken)

    def test_official_notice_binds_history_and_rejects_an_unidentified_detail(self):
        text = ('证券代码：000321 每10股转增6股 股权登记日：2009年7月2日 除权日：2009年7月3日 '
                '新增可流通股份上市日：2009年7月3日 股份到帐日为2009年7月3日；')
        actions = factors.DividendHistory.notice('sz.000321','2009-06-30',text)
        html = ('<table><thead><tr><th colspan="8">分红方案(每10股)</th></tr><tr><th>公告日期</th><th>送股(股)</th>'
                '<th>转增(股)</th><th>派息(税前)(元)</th><th>进度</th><th>除权除息日</th><th>股权登记日</th>'
                '<th>红股上市日</th></tr></thead><tr><td>2009-06-30</td><td>0</td><td>6</td><td>0</td>'
                '<td>实施</td><td>2009-07-03</td><td>2009-07-02</td><td>2009-07-03</td></tr></table>')
        detail = ('<title>000321</title><table><tr><td>登记日</td><td>2009-07-01</td></tr>'
                  '<tr><td>除息日</td><td>2009-07-03</td></tr><tr><td>转增比例（10转增）</td><td>3.4</td></tr></table>')
        base = dict(schema=1,kind='dividend',code='sz.000321',start=None,end=None,year='2009',source_hash='a'*64,records=[])
        resolve = lambda day, registered:{'records':actions,'source':text}
        result = factors.DividendHistory.normalize(base,html,load=lambda url:detail,resolve=resolve)
        self.assertEqual(result['records'], actions)
        same_dates = detail.replace('2009-07-01','2009-07-02').replace('3.4','6')
        self.assertEqual(factors.DividendHistory.normalize(base,html,load=lambda url:same_dates,resolve=resolve)['records'],actions)
        empty_dates = detail.replace('2009-07-01','--').replace('2009-07-03','--').replace('3.4','6')
        self.assertEqual(factors.DividendHistory.normalize(base,html,load=lambda url:empty_dates,resolve=resolve)['records'],actions)
        with self.assertRaises(ValueError):
            factors.DividendHistory.normalize(base,html,load=lambda url:detail.replace('3.4','--'),resolve=resolve)

class CashNoticeTests(unittest.TestCase):
    def test_cash_notice_requires_explicit_payment_and_uses_official_date(self):
        text = ('证券代码：000456 分红派息公告 向全体股东每10股派1.20元人民币现金（含税，'
                '扣税后每10股派1.08元现金）。本次分红派息股权登记日为：2010年7月14日，'
                '除息日为2010年7月15日。本次社会公众股股息于2010年7月16日通过股东托管证券商直接划入其资金帐户；')
        rows = factors.DividendHistory.notice('sz.000456','2010-07-11',text)
        self.assertEqual(rows[0]['announce_date'],'2010-07-11')
        self.assertEqual(rows[0]['cash_per_share'],.12)
        self.assertEqual(rows[0]['payment_date'],'2010-07-16')
        with self.assertRaises(ValueError):
            factors.DividendHistory.notice('sz.000456','2010-07-11',text.replace('直接划入其资金帐户','没有到账证据'))

class AnnouncementIdentityTests(unittest.TestCase):
    def test_code_query_uses_official_org_identity_instead_of_name_search(self):
        from urllib.parse import parse_qs
        calls=[]
        def transport(url, data=None):
            calls.append((url,data))
            if data is None:
                return {'stockList':[{'code':'002345','orgId':'actual-org','zwjc':'新名称'}]}
            return {'totalRecordNum':0,'hasMore':False,'announcements':[]}
        packet=factors.CninfoAnnouncements({},transport).collect({'start':'2010-01-01','end':'2010-01-02','codes':['sz.002345']})
        query=parse_qs(calls[1][1].decode(),keep_blank_values=True)
        self.assertEqual(query['stock'],['002345,actual-org'])
        self.assertEqual(query['searchkey'],[''])
        self.assertEqual(len(packet['raw']),2)
        with self.assertRaises(factors.CollectionError):
            factors.CninfoAnnouncements({},transport).collect({'start':'2010-01-01','end':'2010-01-02','codes':['sz.002346']})

class SettlementNoticeTests(unittest.TestCase):
    def test_identity_and_cash_settlement_use_explicit_official_fields(self):
        text = ('股票代码：000456 每10股派1.20元人民币现金（含税）。股权登记日为：2010年7月14日，'
                '除权除息日为：2010年7月15日。本公司此次委托中国结算深圳分公司代派的股息将于2010年7月16日'
                '通过股东托管证券公司（或其他托管机构）直接划入其资金账户。')
        row=factors.DividendHistory.notice('sz.000456','2010-07-11',text)[0]
        self.assertEqual(row['payment_date'],'2010-07-16')
        self.assertEqual(row['cash_per_share'],.12)

class PaymentTests(unittest.TestCase):
    def test_cash_payment_requires_unique_identity_dates_and_amount(self):
        html = ('<title>SZ.000456 测试 - 派息</title><table><tr><td>公布日期</td><td>财政年度</td>'
                '<td>事项</td><td>股权登记日</td><td>除权除息日</td><td>红利发放日</td></tr>'
                '<tr><td>18/04/2020</td><td>31/12/2019</td><td>10股派1.200元(含稅)</td>'
                '<td>02/07/2020</td><td>03/07/2020</td><td>06/07/2020</td></tr></table>')
        args=('sz.000456',('2020-07-02','2020-07-03'),factors.Decimal('1.2'))
        self.assertEqual(factors.DividendHistory.payment(*args,html),'2020-07-06')
        for broken in (html.replace('000456','000457'), html.replace('1.200','1.201'),
                       html.replace('06/07/2020','--'), html.replace('02/07/2020','01/07/2020'),
                       html.replace('</table>',html[html.index('<tr><td>18/04'):html.index('</table>')]+'</table>')):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                factors.DividendHistory.payment(*args,broken)

    def test_missing_cash_payment_uses_explicit_settlement_source(self):
        html=('<table><thead><tr><th colspan="8">分红方案(每10股)</th></tr><tr>'
              '<th>公告日期</th><th>送股(股)</th><th>转增(股)</th><th>派息(税前)(元)</th>'
              '<th>进度</th><th>除权除息日</th><th>股权登记日</th><th>红股上市日</th></tr></thead>'
              '<tbody><tr><td>2020-06-29</td><td>0</td><td>0</td><td>1.2</td><td>实施</td>'
              '<td>2020-07-03</td><td>2020-07-02</td><td>--</td></tr></tbody></table>')
        detail=('<title>000456明细</title><table><tr><td>登记日</td><td>2020-07-02</td></tr>'
                '<tr><td>除息日</td><td>2020-07-03</td></tr><tr><td>上市日</td><td>--</td></tr>'
                '<tr><td>红利/配股起始日（送、转股到账日）</td><td>--</td></tr></table>')
        base=dict(schema=1,kind='dividend',code='sz.000456',start=None,end=None,year='2020',source_hash='a'*64,records=[])
        def pay(key, amount):
            self.assertEqual(key,('2020-07-02','2020-07-03'))
            self.assertEqual(amount,factors.Decimal('1.2'))
            return {'payment_date':'2020-07-06','source':'explicit payment table'}
        result=factors.DividendHistory.normalize(base,html,load=lambda url:detail,pay=pay)
        self.assertEqual(result['records'][0]['payment_date'],'2020-07-06')
        with self.assertRaises(ValueError):
            factors.DividendHistory.normalize(base,html,load=lambda url:detail)

class CashPaymentNoticeTests(unittest.TestCase):
    def test_delegated_a_share_cash_payment_is_explicit(self):
        text=('证券代码：000456 每10股派0.600000元人民币现金（含税）。'
              '股权登记日为：2024年9月3日，除权除息日为：2024年9月4日。'
              '本公司此次委托中国结算深圳分公司代派的A股股东现金红利将于2024年9月5日'
              '通过股东托管证券公司（或其他托管机构）直接划入其资金账户。')
        result=factors.DividendHistory.notice('sz.000456','2024-08-28',text)
        self.assertEqual(result[0]['payment_date'],'2024-09-05')
        with self.assertRaises(ValueError):
            factors.DividendHistory.notice('sz.000456','2024-08-28',text.replace('2024年9月5日','待定'))

class FactWorkTests(unittest.TestCase):
    def test_years_share_one_history_parse(self):
        import pandas as pd
        html=('<title>000420分红配股</title><table id="sharebonus_1"><thead><tr>'
              '<th colspan="8">分红方案(每10股)</th></tr><tr><th>公告日期</th><th>送股(股)</th>'
              '<th>转增(股)</th><th>派息(税前)(元)</th><th>进度</th><th>除权除息日</th>'
              '<th>股权登记日</th><th>红股上市日</th></tr></thead><tbody><tr><td>2020-04-01</td>'
              '<td>0</td><td>0</td><td>0</td><td>不分配</td><td>--</td><td>--</td><td>--</td>'
              '</tr></tbody></table>')
        response=SimpleNamespace(raise_for_status=lambda:None,text=html)
        with patch('requests.get',return_value=response), patch('pandas.read_html',wraps=pd.read_html) as parse:
            raw,facts=factors.DividendHistory.collect('sz.000420',['2019','2020','2021'],end='2021-12-31')
        self.assertEqual(parse.call_count,1)
        self.assertEqual([f['records'] for f in facts.values()],[[],[],[]])

    def test_years_write_one_shared_evidence(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003',))
            bus=build_data(root);manifest=bus.manifest('fixture')
            for year in ('2019','2020'):
                bus.delete('sources','fixture',f'corp/dividend_sh.600003_{year}.json')
            raw={'html':'shared evidence'*1000,'details':{}}
            def collect(code,years,*,end,**kwargs):
                return raw,{year:dict(schema=1,kind='dividend',code=code,start=None,end=None,year=year,
                                      source_hash='a'*64,records=[]) for year in years}
            with patch.object(factors.DividendHistory,'collect',side_effect=collect), patch.object(bus,'write',wraps=bus.write) as write:
                with bus.transaction('sources'):
                    factors.Methods(bus).complete_facts('fixture',manifest,{'codes':['sh.600003'],'start':'2020-01-31','end':'2020-02-04'})
            logs=[c for c in write.call_args_list if c.args[:2]==('state','collect') and not c.args[2].startswith('coverage/')]
            self.assertEqual(len(logs),1)

    def test_doctor_keeps_successful_stocks_and_retries_only_failures(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003','600004','600005'))
            bus=build_data(root);manifest=bus.manifest('fixture')
            originals={}
            for code in ('sh.600003','sh.600004','sh.600005'):
                name=manifest['inputs']['adjustments']['template'].format(symbol=code,start=manifest['inputs']['adjustments']['start'],end=manifest['inputs']['adjustments']['end'])
                originals[code]=json.loads(bus.read('sources','fixture',name));bus.delete('sources','fixture',name)
            calls=[]
            def collect(task,security,**kwargs):
                calls.append(task.code)
                if task.code=='sh.600004':raise factors.ProviderError('unavailable')
                return {},originals[task.code]
            methods=factors.Methods(bus);source=dict(id='prices',method='daily',dataset_id='fixture')
            with patch.object(factors.SinaFactors,'collect',side_effect=collect):
                with self.assertRaisesRegex(factors.CollectionError,'sh.600004'):
                    methods.doctor(source,{'codes':list(originals)})
            self.assertEqual(calls,list(originals))
            calls.clear()
            with patch.object(factors.SinaFactors,'collect',side_effect=lambda task,security,**kwargs:(calls.append(task.code) or {},originals[task.code])):
                self.assertEqual(methods.doctor(source,{'codes':list(originals)})['status'],'complete')
            self.assertEqual(calls,['sh.600004'])

    def test_discovered_changes_do_not_overwrite_security_file(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003',))
            bus=build_data(root);manifest=bus.manifest('fixture');code='sh.600003'
            security_name=manifest['inputs']['securities']['template'].format(symbol=code)
            security=bus.read('sources','fixture',security_name)
            name=manifest['inputs']['adjustments']['template'].format(symbol=code,start=manifest['inputs']['adjustments']['start'],end=manifest['inputs']['adjustments']['end'])
            original=json.loads(bus.read('sources','fixture',name));bus.delete('sources','fixture',name)
            changes=json.loads(bus.read('sources','fixture','code_changes.json'))
            changes['scope']='discovered_fixture'
            with patch.object(factors.Catalogue,'code_changes',return_value=({'query':'evidence'},changes)), patch.object(factors.SinaFactors,'collect',return_value=({},original)):
                factors.Methods(bus).doctor(dict(id='prices',method='daily',dataset_id='fixture'),{'codes':[code]})
            self.assertEqual(bus.read('sources','fixture',security_name),security)
            self.assertEqual(json.loads(bus.read('sources','fixture','code_changes.json')),changes)

    def test_doctor_rejects_the_whole_invalid_range_before_any_fetch(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003','600004'))
            bus=build_data(root);manifest=bus.manifest('fixture')
            for code in ('sh.600003','sh.600004'):
                name=manifest['inputs']['adjustments']['template'].format(symbol=code,start=manifest['inputs']['adjustments']['start'],end=manifest['inputs']['adjustments']['end'])
                bus.delete('sources','fixture',name)
            name=manifest['inputs']['securities']['template'].format(symbol='sh.600004')
            future=json.loads(bus.read('sources','fixture',name));future['records'][0]['listing_date']='2020-02-06'
            bus.write('sources','fixture',name,json.dumps(future).encode())
            with patch.object(factors.SinaFactors,'collect',side_effect=AssertionError('network must not start')) as fetch:
                with self.assertRaisesRegex(ValueError,'sh.600004.*2020-02-06'):
                    factors.Methods(bus).doctor(dict(id='prices',method='daily',dataset_id='fixture'),{'codes':['sh.600003','sh.600004']})
            self.assertEqual(fetch.call_count,0)

    def test_advancing_the_cutoff_rechecks_only_affected_dividend_years(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003','600004'))
            bus=build_data(root);manifest=bus.manifest('fixture')
            name=manifest['inputs']['securities']['template'].format(symbol='sh.600004')
            stock=json.loads(bus.read('sources','fixture',name))
            stock['records'][0].update(delisting_date='2020-02-01',status='0')
            bus.write('sources','fixture',name,json.dumps(stock).encode())
            tasks=factors.Methods(bus).plan_facts('fixture',manifest,{'codes':['sh.600003','sh.600004'],'start':'2020-02-06','end':'2020-02-06'})
            dividends=[t for _,t in tasks if t.kind=='dividend']
            self.assertEqual([(t.code,t.year) for t in dividends],[('sh.600003','2020')])

    def test_dividend_cutoff_receipt_avoids_refetch_after_partial_range_failure(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003',))
            bus=build_data(root);manifest=bus.manifest('fixture');methods=factors.Methods(bus)
            request={'codes':['sh.600003'],'start':'2020-02-06','end':'2020-02-06'}
            def dividends(code,years,*,end,**kwargs):
                return {},{year:dict(schema=1,kind='dividend',code=code,start=None,end=None,year=year,source_hash='a'*64,records=[]) for year in years}
            task=factors.Request('adjust','sh.600003','1990-01-01','2020-02-06',None)
            adjustment=dict(schema=1,kind='adjust',code=task.code,start=task.start,end=task.end,year=None,source_hash='a'*64,records=[{'date':'1990-01-01','forward':1.,'backward':1.}])
            with patch.object(factors.SinaFactors,'collect',return_value=({},adjustment)),patch.object(factors.DividendHistory,'collect',side_effect=dividends):
                with bus.transaction('sources'):methods.complete_facts('fixture',manifest,request)
            self.assertEqual(methods.plan_facts('fixture',manifest,request),[])
            name=manifest['inputs']['distributions']['template'].format(symbol='sh.600003',year='2020')
            fact=json.loads(bus.read('sources','fixture',name));fact['source_hash']='b'*64
            bus.write('sources','fixture',name,json.dumps(fact).encode())
            self.assertEqual([t.kind for _,t in methods.plan_facts('fixture',manifest,request)],['dividend'])

    def test_fact_sources_share_the_methods_http_session(self):
        from tests.history_fixture import make_snapshot
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);make_snapshot(root,symbols=('600003',))
            bus=build_data(root);manifest=bus.manifest('fixture');methods=factors.Methods(bus)
            task=factors.Request('adjust','sh.600003','1990-01-01','2020-02-06',None)
            adjustment=dict(schema=1,kind='adjust',code=task.code,start=task.start,end=task.end,year=None,source_hash='a'*64,records=[{'date':'1990-01-01','forward':1.,'backward':1.}])
            def dividends(code,years,*,end,get):
                return {},{year:dict(schema=1,kind='dividend',code=code,start=None,end=None,year=year,source_hash='a'*64,records=[]) for year in years}
            with patch.object(factors.SinaFactors,'collect',return_value=({},adjustment)) as adjust,patch.object(factors.DividendHistory,'collect',side_effect=dividends) as dividend:
                with bus.transaction('sources'):methods.complete_facts('fixture',manifest,{'codes':['sh.600003'],'start':'2020-02-06','end':'2020-02-06'})
            self.assertEqual(adjust.call_args.kwargs['get'],dividend.call_args.kwargs['get'])

class RenameFactorTests(unittest.TestCase):
    def test_archived_code_uses_proven_successor_history_on_one_basis(self):
        fixture=SinaFactorTests();fixture.setUp()
        change=dict(exchange='SZ',old_code='000111',new_code='000420',effective_date='2016-01-04',
            source_url='https://static.cninfo.com.cn/finalpage/2016-01-01/test.PDF',
            holder_share_ratio=1,holding_period_continues=True)
        request=factors.Request('adjust','sz.000111','1990-01-01','2026-10-13',None)
        security={**fixture.security,'code':'sz.000111','records':[
            {**fixture.security['records'][0],'delisting_date':'2016-01-04','status':'0'}]}
        old=factors.SinaFactors.normalize(request,security,fixture.payloads(),changes=[change])
        new=factors.SinaFactors.normalize(fixture.request,fixture.security,fixture.payloads(),changes=[change])
        self.assertEqual(old['code'],'sz.000111')
        self.assertEqual(old['records'][-1]['backward'],2.)
        self.assertTrue(factors.SinaFactors.verify_renames({'events':[change]},
            {'sz.000111':old,'sz.000420':new})['events'][0]['factor_basis_continuity_verified'])
        with self.assertRaises(ValueError):
            factors.SinaFactors.normalize(request,security,fixture.payloads(),changes=[{**change,'holder_share_ratio':2}])

    def test_registered_rename_adds_a_source_based_seam_anchor(self):
        fixture=SinaFactorTests();fixture.setUp()
        change={'exchange':'SZ','old_code':'000111','new_code':'000420','effective_date':'2016-01-04'}
        result=factors.SinaFactors.normalize(fixture.request,fixture.security,fixture.payloads(),changes=[change])
        anchor=next(r for r in result['records'] if r['date']=='2016-01-04')
        self.assertEqual(anchor,{'date':'2016-01-04','forward':.5,'backward':2.})
        unchanged=factors.SinaFactors.normalize(fixture.request,fixture.security,fixture.payloads(),changes=[{**change,'new_code':'000421'}])
        self.assertNotIn('2016-01-04',[r['date'] for r in unchanged['records']])

    def test_rename_verdict_requires_explicit_identity_and_equal_factor_bases(self):
        fixture=SinaFactorTests();fixture.setUp()
        change={'exchange':'SZ','old_code':'000111','new_code':'000420','effective_date':'2016-01-04',
                'source_url':'https://static.cninfo.com.cn/finalpage/2016-01-01/test.PDF',
                'holder_share_ratio':1,'holding_period_continues':True,'factor_basis_continuity_verified':False}
        new=factors.SinaFactors.normalize(fixture.request,fixture.security,fixture.payloads(),changes=[change])
        old={**new,'code':'sz.000111','records':[r for r in new['records'] if r['date']<'2016-01-04']}
        result=factors.SinaFactors.verify_renames({'schema':1,'events':[change]}, {'sz.000111':old,'sz.000420':new})
        self.assertTrue(result['events'][0]['factor_basis_continuity_verified'])
        self.assertFalse(change['factor_basis_continuity_verified'])
        wrong={**new,'records':[{**r,'backward':r['backward']*2} for r in new['records']]}
        with self.assertRaises(ValueError):
            factors.SinaFactors.verify_renames({'schema':1,'events':[change]}, {'sz.000111':old,'sz.000420':wrong})
        with self.assertRaises(ValueError):
            factors.SinaFactors.verify_renames({'schema':1,'events':[{**change,'holder_share_ratio':2}]}, {'sz.000111':old,'sz.000420':new})

class RenameNoticeTests(unittest.TestCase):
    def test_official_terms_identify_codes_date_and_unchanged_holdings(self):
        text=('公司证券代码由“000111/200111”变更为“001456/201456”。'
              '变更后的证券简称和证券代码启用日期：2024年9月4日。'
              '投资者证券账户持有本公司股份数量不变。个人投资者持股期限将持续计算。')
        rows=factors.Catalogue.rename('SZ','2024-09-03','https://static.cninfo.com.cn/finalpage/2024-09-03/test.PDF',text)
        self.assertEqual([(r['old_code'],r['new_code']) for r in rows],[('000111','001456'),('200111','201456')])
        self.assertEqual(rows[0]['effective_date'],'2024-09-04')
        split=text.replace('持股期限将','持股期限\n5/7\n将')
        self.assertEqual(factors.Catalogue.rename('SZ','2024-09-03',rows[0]['source_url'],split),rows)
        for broken in (text.replace('股份数量不变','股份数量另行确定'),text.replace('持股期限将持续计算','持股期限另行确定'),text.replace('2024年9月4日','待定')):
            with self.subTest(text=broken),self.assertRaises(ValueError):
                factors.Catalogue.rename('SZ','2024-09-03','https://static.cninfo.com.cn/finalpage/2024-09-03/test.PDF',broken)

    def test_code_change_collection_uses_official_records_without_manual_events(self):
        text=('公司证券代码由“000111”变更为“001456”。'
              '变更后的证券简称和证券代码启用日期：2024年9月4日。'
              '投资者持有本公司股份数量不变。持股期限将持续计算。')
        rows=[{'code':'sz.000111','ipoDate':'1994-01-01','outDate':'2024-09-04'},
              {'code':'sz.001456','ipoDate':'1994-01-01','outDate':''}]
        packet={'records':[{'code':'000111','title':'证券代码变更实施公告',
                           'url':'https://static.cninfo.com.cn/finalpage/2024-09-03/example.PDF'}],'raw':[]}
        response=SimpleNamespace(raise_for_status=lambda:None,content=b'%PDF-example')
        pages=SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:text)])
        with patch.object(factors.CninfoAnnouncements,'collect',return_value=packet),patch('requests.get',return_value=response),patch('pypdf.PdfReader',return_value=pages):
            raw,result=factors.Catalogue.code_changes({'schema':1,'events':[]},rows,{'sz.000111'},'2024-01-01','2024-09-04')
        self.assertEqual(result['events'][0]['new_code'],'001456')
        self.assertEqual(result['events'][0]['effective_date'],'2024-09-04')
        self.assertEqual(raw['query'],packet)
        with patch.object(factors.CninfoAnnouncements,'collect',side_effect=AssertionError('repeat query')):
            self.assertEqual(factors.Catalogue.code_changes(result,rows,{'sz.000111'},'2024-01-01','2024-09-04'),(None,result))
