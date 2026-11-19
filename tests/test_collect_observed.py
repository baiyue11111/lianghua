"""Collection saves observations; completeness is a separate question."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from collect.methods import Methods, SinaDaily
from market import build_data
from protocol import DataError, Filter, TableQuery
from tests.history_fixture import make_snapshot

class ObservedTests(unittest.TestCase):
    def test_record_conflicts_keep_other_codes_and_rollback_all_rejected_code_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            bus=build_data(Path(folder)); methods=Methods(bus)
            source={'id':'quotes','method':'fixture','params':{'key_fields':['code','date']}}
            packet={'records':[{'code':'A','date':'2020-01-01','close':10.}], 'raw':[], 'scope':{}}
            methods._deliver(source,packet,dry_run=False)
            result=methods._deliver(source,{**packet,'records':[
                {'code':'A','date':'2020-01-02','close':12.},
                {'code':'A','date':'2020-01-01','close':11.},
                {'code':'C','date':'2020-01-01','close':10.}]},dry_run=False)
            self.assertEqual(result['records'],1)
            self.assertEqual([item['code'] for item in result['failures']],['A'])
            self.assertEqual(bus.table(bus.source('quotes', 'records'), order=('code', 'date')).to_pylist(),
                [{'code':'A','date':'2020-01-01','close':10.}, {'code':'C','date':'2020-01-01','close':10.}])
            document={**source,'id':'documents','params':{'key_fields':['id']}}
            methods._deliver(document,{**packet,'records':[{'id':'one','text':'old'}]},dry_run=False)
            with self.assertRaises(DataError):
                methods._deliver(document,{**packet,'records':[{'id':'one','text':'new'}]},dry_run=False)

    def test_facts_conflicts_keep_other_codes_and_preserve_stored_version(self):
        handler=SimpleNamespace(text_columns=('code','date','period','visible_at'),
            value_columns={'value':('VALUE','ratio',False)}, dates=('period','visible_at'),
            keys=('code','period','visible_at'), validate_record=lambda row:None)
        with tempfile.TemporaryDirectory() as folder:
            bus=build_data(Path(folder)); methods=Methods(bus,{'fixture':handler})
            source={'id':'facts','method':'fixture','data_type':'facts'}
            row={'code':'sh.600003','date':'2020-01-01','period':'2019-09-30',
                 'visible_at':'2020-01-01T00:00:00+08:00','value':.2}
            packet={'records':[row],'raw':[],'scope':{}}
            methods._deliver(source,packet,dry_run=False)
            result=methods._deliver(source,{**packet,'records':[{**row,'value':.3},
                {**row,'code':'sh.600004','value':.4}]},dry_run=False)
            self.assertEqual(result['records'],1)
            self.assertEqual([item['code'] for item in result['failures']],['sh.600003'])
            rows=bus.table(bus.source('facts', 'records'), order=('code',)).to_pylist()
            self.assertEqual([(item['code'],item['value']) for item in rows],[('sh.600003',.2),('sh.600004',.4)])

    def setup_store(self, folder):
        root=Path(folder);make_snapshot(root,symbols=('600003',))
        bus=build_data(root)
        source={'id':'daily','method':'sina_daily','data_type':'daily','storage_id':'fixture','dataset_id':'fixture',
                'fields':['open','high','low','close','volume','amount']}
        return bus,Methods(bus,{}),source

    def bars(self, bus, code):
        return bus.table(bus.source('fixture', 'bars'), filters=(Filter('code', 'eq', code),),
                         order=('date',)).to_pylist()

    def packet(self, rows, **request):
        return {'data_type':'daily','records':rows,'raw':[{'url':'https://source.example/daily','response':'raw'}],
                'scope':{'codes':['sh.600003'],'start':'2020-01-31','end':'2020-02-03'},
                'request':{'codes':['sh.600003'],'start':'2020-01-31','end':'2020-02-03',**request},
                'calendar':{'sessions':['2020-01-31','2020-02-03','2020-02-04'],
                            'coverage':[{'start':'2020-01-31','end':'2020-02-04'}]}}

    def test_partial_fields_and_zero_are_saved_without_invented_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            code = 'sh.600004'
            m.publish(source, self.packet([
                {'code': code, 'date': '2020-01-31', 'close': 12., 'amount': None},
                {'code': code, 'date': '2020-02-03', 'volume': 0.},
            ], codes=[code]))
            rows = self.bars(bus, code)
            self.assertEqual([(r['date'], r['close'], r['volume'], r['amount']) for r in rows],
                             [('2020-01-31', 12., None, None), ('2020-02-03', None, 0., None)])

    def test_append_only_bars_reject_refresh_without_changing_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            code = 'sh.600004'
            m.publish(source, self.packet([
                {'code': code, 'date': '2020-01-31', 'close': 12., 'amount': 50.},
                {'code': code, 'date': '2020-02-03', 'close': 13., 'amount': 60.},
            ], codes=[code]))
            before = self.bars(bus, code)
            with self.assertRaises(DataError) as caught:
                m.publish(source, self.packet([{'code': code, 'date': '2020-01-31', 'close': 14.}],
                                              codes=[code], refresh=True))
            self.assertEqual(caught.exception.code, 'unsupported_refresh')
            self.assertEqual(self.bars(bus, code), before)

    def test_valid_empty_response_writes_evidence_only(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            before = bus.calendar('fixture'), self.bars(bus, 'sh.600003')
            packet = self.packet([])
            self.assertEqual(m.publish(source, packet)['records'], 0)
            self.assertEqual((bus.calendar('fixture'), self.bars(bus, 'sh.600003')), before)
            self.assertFalse(m.daily_coverage('fixture',('sh.600999',),date(2020,1,31),date(2020,2,3),('close',))['complete'])
            rows = bus.scan(TableQuery('collection_audit')).table.to_pylist()
            events = [json.loads(row['settings']) for row in rows]
            self.assertEqual([event['raw'] for event in events], [packet['raw']])
            self.assertEqual(events[0]['incoming'], [])

    def test_bad_identity_date_value_and_duplicate_are_rejected_before_publish(self):
        good={'code':'sh.600003','date':'2020-01-31','close':12.}
        for changes in ({'code':'sh.600004'},{'date':'bad'},{'date':'2020-02-04'}, {'close':float('nan')},{'close':-1.}):
            with self.subTest(changes=changes),self.assertRaises((DataError,ValueError)):
                Methods.validate(self.packet([{**good,**changes}]))
        with self.assertRaises(DataError):Methods.validate(self.packet([good,{**good,'close':13.}]))

    def test_sina_empty_partial_and_subset_need_only_the_daily_source(self):
        request={'codes':['sz.000456'],'start':'2020-01-31','end':'2020-02-03','fields':['close'],'adjustment':'3'}
        calls=[]
        with patch.object(SinaDaily,'decode',return_value=[]):
            packet=SinaDaily({},transport=lambda url:calls.append(url) or 'raw').collect(request)
        self.assertEqual(packet['records'],[])
        self.assertEqual(len(calls),1);self.assertIn('finance.sina.com.cn',calls[0])
        with patch.object(SinaDaily,'decode',return_value=[{'date':'2020-01-31','close':10.,'volume':50.}]):
            packet=SinaDaily({},transport=lambda url:'raw').collect(request)
        self.assertEqual(packet['records'],[{'code':'sz.000456','date':'2020-01-31','close':10.,'raw_page':0}])

    def test_last_dates_are_per_security_and_do_not_infer_missing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            m.publish(source,self.packet([{'code':'sh.600003','date':'2020-01-31','close':12.}]))
            self.assertEqual(m.last_dates(source,['600003','sh.600999']),{'600003':'2020-02-04'})

    def test_a_stored_bar_is_kept_and_the_rest_of_the_packet_lands(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            m.publish(source,self.packet([{'code':'sh.600003','date':'2020-02-05','close':12.}],
                                         start='2020-02-03',end='2020-02-06'))
            # The snapshot already holds 2020-02-03 close 11; the packet sends a
            # different value for it alongside two bars nobody has.
            packet=self.packet([{'code':'sh.600003','date':'2020-02-03','close':14.},
                                {'code':'sh.600003','date':'2020-02-06','close':15.},
                                {'code':'sh.600004','date':'2020-02-05','close':20.}],
                               start='2020-02-03',end='2020-02-06')
            packet['request']['codes'].append('sh.600004')
            result=m.publish(source,packet)
            self.assertEqual(result['failures'],[])
            # ``records`` counts the bars the source delivered; the overlapping
            # one is skipped at the store, not refused.
            self.assertEqual(result['records'],3)
            rows=[row for row in m.data.table(m.data.source('fixture','bars')).to_pylist()
                  if row['date'] >= '2020-02-03']
            self.assertEqual(sorted((r['code'],r['date'],r['close']) for r in rows),
                             [('sh.600003','2020-02-03',11.),('sh.600003','2020-02-04',12.),
                              ('sh.600003','2020-02-05',12.),('sh.600003','2020-02-06',15.),
                              ('sh.600004','2020-02-05',20.)])

    def test_partial_rows_are_readable_in_collection_view(self):
        with tempfile.TemporaryDirectory() as folder:
            bus,m,source=self.setup_store(folder)
            m.publish(source,self.packet([{'code':'sh.600003','date':'2020-01-31','close':10.}]))
            view=m.records(source,0,500)
            self.assertTrue(any(row['close']==10. for row in view['records']))

    def test_provider_record_validation_runs_before_batch_publication(self):
        class Provider:
            def __init__(self, config):pass
            @staticmethod
            def validate_record(row):
                if row['roe'] < 0:raise ValueError('bad report')
            def collect(self, request):
                return {'records':[{'code':request['codes'][0],'roe':-1.}],
                        'raw':[{'response':'bad report original'}],'scope':{}}
        with tempfile.TemporaryDirectory() as folder:
            bus=build_data(Path(folder));m=Methods(bus,{'probe':Provider})
            from collect.methods import CollectionError
            with self.assertRaises(CollectionError) as caught:
                m.fetch({'id':'reports','method':'probe'},{'codes':['sh.600003']})
            self.assertEqual(caught.exception.evidence,[{'response':'bad report original'}])

    def test_duplicate_partial_rows_validate_their_combined_observation(self):
        base={'code':'sh.600003','date':'2020-01-31'}
        with self.assertRaises(DataError):
            Methods.validate(self.packet([{**base,'close':20.},{**base,'high':12.}]))
        merged=Methods.daily_rows(self.packet([{**base,'close':10.},{**base,'high':12.}]))
        self.assertEqual(merged[('sh.600003','2020-01-31')],{**base,'close':10.,'high':12.})
