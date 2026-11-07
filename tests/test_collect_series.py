"""Public close-only series use the existing daily store and comparison port."""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from collect import methods
from market import build_data
from protocol import DataError, normalize_security_id


class SeriesTests(unittest.TestCase):
    def provider(self, text):
        return methods.FredSeries({}, transport=lambda url: text)

    def test_identity_dates_missing_observations_and_raw_evidence(self):
        text='observation_date,OTHER\n2020-01-30,100\n2020-01-31,.\n2020-02-03,102\n'
        request={'codes':['fred.other'],'fields':['close'],'start':'2020-01-30','end':'2020-02-03'}
        packet=self.provider(text).collect(request)
        self.assertEqual(packet['records'],[
            {'code':'fred.other','date':'2020-01-30','close':100.},
            {'code':'fred.other','date':'2020-02-03','close':102.}])
        self.assertEqual(packet['calendar']['sessions'],['2020-01-30','2020-02-03'])
        self.assertEqual(packet['raw'][0]['response'],text)
        self.assertIn('id=OTHER',packet['raw'][0]['request']['url'])
        self.assertEqual(normalize_security_id('FRED.NASDAQCOM'),'fred.nasdaqcom')

    def test_wrong_identity_duplicates_bad_prices_and_dates_are_rejected(self):
        request={'codes':['fred.other'],'fields':['close'],'start':'2020-01-30','end':'2020-02-03'}
        for text in ('observation_date,WRONG\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-01-30,100\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-02-03,102\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-01-30,NaN\n',
                     'observation_date,OTHER\n2020-01-30,0\n',
                     'observation_date,OTHER\n2020-02-30,100\n',
                     'observation_date,OTHER\n2020-02-04,100\n',
                     'observation_date,OTHER\n2020-01-30,.\n'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                self.provider(text).collect(request)

    def test_close_only_publication_is_readable_but_not_tradable(self):
        from tests.history_fixture import make_snapshot
        from protocol import DataQuery
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);make_snapshot(root,symbols=('600003',));bus=build_data(root)
            source={'id':'series','method':'fred_series','data_type':'daily','dataset_id':'fixture',
                    'storage_id':'series','fields':['close'],'defaults':{'codes':['fred.other']}}
            fetch=methods.Methods(bus,{'fred_series':lambda source,request:self.provider(
                'observation_date,OTHER\n2020-01-31,100\n2020-02-03,102\n2020-02-04,103\n').collect(request)})
            request=fetch.prepare_request(source,{'codes':['fred.other'],'start':'2020-01-31','end':'2020-02-04'})
            packet=fetch.fetch(source,request);fetch.publish(source,packet)
            view=fetch.records(source,0,10)
            self.assertEqual(view['records_count'],3)
            self.assertEqual({k for k,v in view['records'][0].items() if v is not None},{'code','date','close'})
            query=DataQuery('fixture',date(2020,1,31),date(2020,2,3),('close',),1,'raw',
                            symbols=('fred.other',),universe='dataset',purpose='compare')
            result=bus.query(query)
            self.assertEqual([r.frame.fields['fred.other']['close'] for r in result.records],[100.,102.])
            with self.assertRaises((ValueError,DataError)):
                bus.query(DataQuery('fixture',query.start,query.end,('close',),1,'raw',
                                    symbols=('fred.other',),universe='dataset',purpose='trade'))
            before=bus.read('sources','fixture','manifest.json')
            result=fetch.publish({**source,'fields':['open','high','low','close','volume','amount']},packet)
            self.assertEqual(result['records'],3)
            self.assertEqual(bus.read('sources','fixture','manifest.json'),before)


class CrossMarketBenchmarkTests(unittest.TestCase):
    def service(self, *, currency=False):
        from backend import Benchmarks
        days=['2020-01-30','2020-01-31','2020-02-03','2020-02-04']
        class Store:
            def read(self,kind,identity,name):
                return json.dumps({'sessions':days,'coverage':[{'start':days[0],'end':days[-1]}]}).encode()
            def manifest(self,identity):return {'calendar':'calendar.json'}
            def query(self,query):
                prices=[100.,110.,120.,130.] if query.dataset=='index' else [7.,7.1,7.2,7.3]
                return SimpleNamespace(records=[SimpleNamespace(trade_date=date.fromisoformat(day),
                    frame=SimpleNamespace(fields={query.symbols[0]:{'close':price}}))
                    for day,price in zip(days,prices) if query.start<=date.fromisoformat(day)<=query.end],
                    provenance={'data_hash':query.dataset+'-hash'})
        specs=[{'id':'composite','dataset_id':'index','code':'fred.other','currency':'USD',
                'return_type':'total','alignment':'previous_close','timezone':'America/New_York',
                'session_close':'16:00','comparison_timezone':'Asia/Shanghai','comparison_close':'15:00'}]
        if currency:
            specs[0].update(currency='CNY',quote_currency='USD',fx_id='fx')
            specs.append({**specs[0],'id':'fx','dataset_id':'fx','code':'fred.fx','currency':'CNY',
                          'fx_id':None,'quote_currency':'CNY','base_currency':'USD'})
        return Benchmarks(Store(),specs)

    def test_us_same_day_close_cannot_enter_china_close_and_holidays_keep_last_close(self):
        result=self.service().series('composite','2020-01-31','2020-02-03',
                                    dates=('2020-01-31','2020-02-01','2020-02-03'))
        self.assertEqual([p['close'] for p in result['points']],[100.,110.,110.])
        self.assertEqual([p['observed_at'] for p in result['points']],['2020-01-30','2020-01-31','2020-01-31'])
        self.assertEqual(result['return_type'],'total')
        with self.assertRaises(ValueError):
            self.service().series('composite','2020-01-30','2020-01-31',dates=('2020-01-30','2020-01-31'))

    def test_rmb_valuation_keeps_both_source_hashes(self):
        result=self.service(currency=True).series('composite','2020-01-31','2020-02-03',
                                                 dates=('2020-01-31','2020-02-03'))
        self.assertEqual([p['close'] for p in result['points']],[700.,781.])
        self.assertEqual(result['currency'],'CNY')
        self.assertEqual(result['source_hashes'],{'composite':'index-hash','fx':'fx-hash'})

    def test_inverse_fx_rate_cannot_be_silently_multiplied(self):
        service=self.service(currency=True)
        service.specs['fx']['base_currency']='CNY'
        with self.assertRaisesRegex(ValueError,'direction'):
            service.series('composite','2020-01-31','2020-02-03',dates=('2020-01-31','2020-02-03'))
