import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from collect.core import Collect
from collect.methods import Methods
from collect.sources import Sources
from market import build_data
from market.views import Views


class EtfMarketTests(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.data = build_data(root)
        self.addCleanup(self.data.close)
        registry = json.loads((Path(__file__).resolve().parents[1] /
                               'data/source/registry/sources.json').read_text())
        ids = {'daily', 'stock_basic', 'etf_catalogue', 'etf_daily'}
        self.data.update_registry(lambda value: {
            **value, 'markets': [m for m in registry['markets'] if m['scope'] in ('CN_A_SH_SZ', 'ETF_CN')],
            'sources': [source for source in registry['sources'] if source['id'] in ids],
        })
        self.data.save_calendar('cn_a', {
            'sessions': ['2026-11-16', '2026-11-17', '2026-11-18'],
            'coverage': [{'start': '2026-11-16', 'end': '2026-11-18'}],
        })
        self.sources = Sources(self.data)
        self.views = Views(self.data, self.sources)

    def test_directory_and_market_flow_use_native_etf_prices(self):
        import pandas as pd

        directory = pd.DataFrame([{'代码': 'sh510300', '名称': '沪深300ETF'},
                                  {'代码': 'sz159998', '名称': '计算机ETF'}])

        def daily(source, request):
            records = [{'code': code, 'date': request['end'], 'open': 1.0, 'close': 1.2,
                        'low': .9, 'high': 1.3, 'volume': 1200, 'amount': 1320}
                       for code in request['codes']]
            return {'records': records, 'raw': [], 'scope': {}}

        methods = Methods(self.data, {'sina_daily': daily})
        collect = Collect(self.sources, methods)
        self.addCleanup(collect.close)
        stock = {'code': 'sh.600000', 'code_name': '浦发银行', 'ipoDate': '1999-11-10',
                 'outDate': '', 'status': '1'}
        self.data.put_rows('stocks', [stock], keys=('code',))
        self.data.bind('security_catalogue', 'records', 'lake:stocks')
        self.data.put_rows('stock_prices', [
            {'code': 'sh.600000', 'date': '2026-11-17', 'open': 10.0, 'close': 12.0,
             'low': 9.0, 'high': 13.0, 'volume': 12000, 'amount': 132000},
        ], keys=('code', 'date'))
        self.data.bind('cn_a', 'bars', 'lake:stock_prices')
        calendar = self.data.calendar('cn_a')
        self.assertEqual(self.views.handle('data.search', {'q': '浦发'})['items'][0]['最新收盘'], 12.0)
        with patch('akshare.fund_etf_category_sina', return_value=directory) as upstream:
            result = collect.run({'market': 'ETF_CN', 'command': 'latest', 'end': '2026-11-17'})
        upstream.assert_called_once_with(symbol='ETF基金')
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual([job['source_id'] for job in result['jobs']], ['etf_catalogue', 'etf_daily'])
        self.assertEqual(result['jobs'][1]['request']['start'], '2026-11-16')
        record = self.views.handle('data.search', {'q': '510300'})['items'][0]
        self.assertEqual((record['代码'], record['名称'], record['首日'], record['最新收盘']),
                         ('sh.510300', '沪深300ETF', None, 1.2))
        self.assertEqual(self.views.handle('data.search', {'q': '计算机ETF'})['items'][0]['代码'],
                         'sz.159998')
        for code in ('sh.510300', '510300', 'sz.159998', '159998'):
            with self.subTest(code=code):
                self.assertEqual(self.views.handle('data.bars', {'code': code})['candles'],
                                 [[1.0, 1.2, .9, 1.3]])
        self.assertEqual(self.views.handle('data.search', {'q': '599999'}), {'items': []})
        self.assertEqual(self.views.handle('data.bars', {'code': '599999'})['candles'], [])
        self.assertEqual(self.views.handle('data.bars', {'code': 'sh.599999'})['candles'], [])
        with self.assertRaisesRegex(ValueError, '不提供所选复权口径'):
            self.views.handle('data.bars', {'code': '510300', 'adj': '前复权'})
        with self.assertRaisesRegex(ValueError, 'outside the source scope'):
            methods.prepare_request(self.sources.get('etf_daily'), {'codes': ['sh.600000']})
        self.assertEqual(self.data.table(self.data.source('cn_a', 'bars')).to_pylist()[0]['code'], 'sh.600000')
        self.assertEqual(self.data.calendar('cn_a'), calendar)
        self.assertEqual([row['code'] for row in methods.record_rows(self.sources.get('etf_catalogue'))],
                         ['sh.510300', 'sz.159998'])
        self.assertEqual(methods.daily_collection_summary(self.sources.get('etf_daily'), 'cn_a')
                         ['symbols_count'], 2)
