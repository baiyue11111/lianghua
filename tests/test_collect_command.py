"""Market collection commands use the registered market and existing collector."""
import contextlib
import importlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from collect.core import Collect
from collect.methods import Methods
from collect.sources import Sources
from market import DataBus
from protocol import DataError
from backend import Modules, Protocol
from storage import DataStore
from frontend import cli

ui = importlib.import_module('frontend.ui-protocol')


class CollectionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bus = DataBus(DataStore(Path(self.temp.name)))
        self.sources = Sources(self.bus)
        self.sources.register({'id': 'prices', 'name': 'Prices', 'method': 'fixture',
                               'data_type': 'prices', 'frequency': 'daily',
                               'defaults': {}, 'params': {'key_fields': ['code', 'date']}})
        self.bus.update_registry(lambda r: {**r, 'markets': [{
            'scope': 'TEST', 'name': '测试市场', 'timezone': 'Asia/Shanghai',
            'collect': {'default': ['prices'], 'codes': ['X', 'Y'],
                        'data': {'prices': {'source': 'prices'}}}}]})
        self.calls = []
        def fetch(source, request):
            self.calls.append(request)
            return {'records': [{'code': code, 'date': request['end'], 'close': 10.}
                                for code in request['codes']], 'raw': [],
                    'scope': {'end': request['end']}}
        self.collect = Collect(self.sources, Methods(self.bus, {'fixture': fetch}))
        self.addCleanup(self.collect.close)

    def invoke(self, selector, **values):
        from backend import CollectionPort
        class Port:
            def handle(self, operation, data): return {}
        port = Port()
        adapter = CollectionPort(self.collect)
        backend = Protocol(Modules(port, port, adapter, self.collect, port))
        backend.close = lambda: None
        seen = []
        original = backend.send
        async def send(request):
            seen.append(request)
            return await original(request)
        backend.send = send
        action = ui.UiAction('test', 'invoke', 'cli', {'selector': selector, **values}, 'cli.collect')
        with patch('backend.build_backend', return_value=backend):
            state = ui.exchange(action)
        self.assertNotIn('cli.collect', [request.operation for request in seen])
        self.assertTrue(all('selector' not in request.data for request in seen))
        slot = state.targets['cli']
        if slot.problems:
            raise ValueError(slot.problems[0].title)
        return slot.messages[0].data

    def test_year_range_is_resolved_only_in_the_interaction_core(self):
        result=self.invoke('测试市场:[2003,2012]')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.calls[0]['start'],'2003-01-01')
        self.assertEqual(self.calls[0]['end'],'2012-12-31')

    def test_market_range_batches_inside_the_interaction_core(self):
        def change(registry):
            registry['markets'][0]['collect']['batch_size']=1
            return registry
        self.bus.update_registry(change)
        result=self.invoke('测试市场:[2003,2012]')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual([r['codes'] for r in self.calls],[['X'],['Y']])

    def test_invalid_or_ambiguous_range_fetches_nothing(self):
        for selector,values in [('TEST:[2012,2003]',{}),('TEST:[2003,2012]',{'start':'2004-01-01','end':'2004-12-31'}),
                                ('TEST:[2003-02-30,2012-12-31]',{})]:
            with self.assertRaises(ValueError):self.invoke(selector,**values)
        self.assertEqual(self.calls,[])

    def test_range_resolves_market_and_uses_full_registered_codes(self):
        result = self.invoke('测试市场:range', start='2026-10-01', end='2026-10-14')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.calls[0]['codes'], ['X', 'Y'])
        self.assertEqual(self.calls[0]['start'], '2026-10-01')

    def test_catalogue_uses_the_shared_market_code_rule(self):
        self.sources.register({'id': 'catalogue', 'name': 'Catalogue',
                               'method': 'catalogue_fixture', 'data_type': 'catalogue',
                               'frequency': 'snapshot', 'defaults': {},
                               'params': {'key_fields': ['code']}})
        self.collect.methods.handlers['catalogue_fixture'] = lambda source, request: {
            'records': [{'code': 'X'}, {'code': 'Y'}], 'raw': [], 'scope': {}}
        def change(registry):
            market = registry['markets'][0]
            market['code_pattern'] = 'X'
            market['collect'].pop('codes')
            market['collect']['securities'] = {
                'source': 'catalogue', 'code': 'code', 'from': 'listed', 'to': 'delisted'}
            return registry
        self.bus.update_registry(change)
        self.invoke('TEST:range', start='2026-10-01', end='2026-10-14')
        self.assertEqual(self.calls[-1]['codes'], ['X'])

    def test_latest_includes_last_stored_day_and_today(self):
        self.invoke('TEST:range', start='2026-10-01', end='2026-10-14')
        self.invoke('TEST:latest')
        self.assertEqual(self.calls[-1]['start'], '2026-10-14')
        self.assertEqual(self.calls[-1]['end'], datetime.now().astimezone(
            __import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat())
        self.assertTrue(self.calls[-1]['force'])

    def test_empty_latest_invalid_market_and_bad_parameters_never_fetch(self):
        for selector, values in [('TEST:latest',{}),('OTHER:latest',{}),('TEST:other',{}),
            ('TEST:range',{}),('TEST:latest',{'start':'2026-10-01'}),
            ('TEST:range',{'data':'missing','start':'2026-10-01','end':'2026-10-14'})]:
            with self.assertRaises(ValueError): self.invoke(selector,**values)
        self.assertEqual(self.calls,[])

    def test_failed_provider_fails_the_interaction(self):
        def fail(source,request):raise ValueError('provider failed')
        self.collect.methods.handlers['fixture']=fail
        with self.assertRaises(ValueError):
            self.invoke('TEST:range',start='2026-10-01',end='2026-10-14')

    def test_check_reports_incomplete_without_fetching(self):
        with patch.object(self.collect.methods,'coverage',return_value={'complete':False,'fields':('close',)}):
            result=self.invoke('TEST:check',start='2026-10-01',end='2026-10-14')
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['plans'][0]['coverage']['fields'],['close'])
        self.assertEqual(self.calls,[])

    def test_auto_runs_two_cycles_in_core_and_ctrl_c_stops_session(self):
        from backend import CollectionPort
        self.collect.run({'source_id':'prices','codes':['X','Y'],'start':'2026-10-01','end':'2026-10-14'})
        self.calls.clear()
        class Port:
            def handle(self,operation,data):return {}
        port=Port()
        backend=Protocol(Modules(port,port,CollectionPort(self.collect),self.collect,port))
        states=[]
        def progress(state):
            states.append(state)
            if len(states)==2:raise KeyboardInterrupt
        action=ui.UiAction('auto','invoke','cli',{'selector':'TEST:auto','interval':'1s'},'cli.collect')
        with patch('backend.build_backend',return_value=backend):
            with self.assertRaises(KeyboardInterrupt):ui.exchange(action,on_state=progress)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len(states),2)

    def test_today_request_queries_calendar_beyond_end(self):
        from collect.methods import BaostockDaily
        from types import SimpleNamespace
        from datetime import timedelta
        today = datetime.now().date()
        observed = []
        class Provider:
            def login(self): return SimpleNamespace(error_code='0')
            def logout(self): pass
            def query_history_k_data_plus(self, code, fields, **kwargs):
                return SimpleNamespace(error_code='0', fields=fields.split(','), next=lambda: False)
        def calendar(provider, start, end):
            observed.append(end)
            return [today, today+timedelta(days=1)], {}
        import sys
        with patch.dict(sys.modules, {'baostock': Provider()}), patch.object(BaostockDaily, 'trade_calendar', side_effect=calendar):
            BaostockDaily({}).collect({'codes':['sh.600036'],'start':today.isoformat(),'end':today.isoformat()})
        self.assertGreater(observed[0], today.isoformat())

    def test_refresh_updates_overlapping_day_but_default_rejects_conflict(self):
        from collect.methods import Methods
        old = {'close':10., 'amount':20.}
        new = {'close':11., 'amount':None}
        with self.assertRaises(DataError):
            Methods._merge_daily_row(old, new, ('X','2026-10-14'))
        self.assertEqual(Methods._merge_daily_row(old, new, ('X','2026-10-14'), refresh=True),
                         {'close':11.,'amount':20.})

    def test_catalogue_is_an_existing_method_packet_not_private_storage(self):
        from collect import methods
        from types import SimpleNamespace
        fields = ['code','code_name','ipoDate','outDate','type','status']
        rows = [['sh.600036','Example','2002-04-09','','1','1']]
        response = SimpleNamespace(error_code='0', error_msg='', fields=fields,
                                   next=iter([True,False]).__next__, get_row_data=lambda: rows[0])
        provider = SimpleNamespace(login=lambda:SimpleNamespace(error_code='0'),
                                   logout=lambda:None, query_stock_basic=lambda **kwargs:response)
        import sys
        self.assertIn('catalogue', methods.DEFAULT_METHODS)
        with patch.dict(sys.modules, {'baostock':provider}), patch.object(methods.BseCatalogue, 'collect', return_value={'records':[], 'raw':[]}) as bse:
            packet = methods.DEFAULT_METHODS['catalogue']({}).collect({'exchanges':['sh','sz']})
        bse.assert_not_called()
        self.assertEqual(packet['records'][0]['code'], 'sh.600036')
        self.assertEqual(len(packet['raw']), 1)


class CollectCliTests(unittest.TestCase):
    def test_cli_only_passes_arguments_to_interaction_core(self):
        args=cli.build_parser().parse_args(['--collect','TEST:range','--start','2014-01-01','--end','2026-10-14'])
        action=cli.to_action(args)
        self.assertEqual(action.operation,'cli.collect')
        self.assertEqual(action.data['selector'],'TEST:range')
        self.assertNotIn('config',action.data)

    def test_backend_has_no_cli_collection_command(self):
        from backend import export_operations
        self.assertNotIn('cli.collect',export_operations())
        self.assertFalse((Path(__file__).parents[1]/'src/collect/command.py').exists())

class BseSourceTests(unittest.TestCase):
    def page(self, number, total=2):
        return {'number':number,'totalPages':2,'totalElements':total,'numberOfElements':1,
                'content':[{'xxzqdm':f'92000{number}','xxzqjc':'公司','fxssrq':'20240102'}]}

    def test_catalogue_reads_every_page_and_keeps_raw_pages(self):
        from collect.methods import BseCatalogue
        seen=[]
        def page(number):seen.append(number);return self.page(number)
        packet=BseCatalogue({},transport=page).collect({})
        self.assertEqual(seen,[0,1])
        self.assertEqual([r['code'] for r in packet['records']],['bj.920000','bj.920001'])
        self.assertEqual(packet['records'][1]['raw_page'],1)
        self.assertEqual(packet['records'][0]['ipoDate'],'2024-01-02')
        self.assertEqual(len(packet['raw']),2)

    def test_catalogue_rejects_changed_total_and_duplicate_codes(self):
        from collect.methods import BseCatalogue,CollectionError
        for page in [lambda n:self.page(n,2+n), lambda n:self.page(0)]:
            with self.assertRaises(CollectionError):BseCatalogue({},transport=page).collect({})

    def test_bse_daily_converts_lots_to_shares_and_preserves_evidence(self):
        from collect.methods import EastmoneyDaily
        response={'rc':0,'data':{'code':'920000','klines':[
            '2026-10-16,10,11,12,9,23,24000,30,10,1,2']}}
        seen=[]
        def get(url):seen.append(url);return response
        packet=EastmoneyDaily({},transport=get).collect({'codes':['bj.920000'],
            'start':'2026-10-16','end':'2026-10-16',
            'fields':['open','high','low','close','volume','amount','pctChg','turn']})
        self.assertEqual(packet['records'][0]['volume'],2300)
        self.assertEqual(packet['records'][0]['amount'],24000)
        self.assertEqual(packet['records'][0]['code'],'bj.920000')
        self.assertEqual(packet['raw'][0]['response'],response)
        self.assertIn('secid=0.920000',seen[0])

    def test_daily_rejects_unknown_fields_bad_rows_and_wrong_identity(self):
        from collect.methods import EastmoneyDaily,CollectionError
        request={'codes':['bj.920000'],'start':'2026-10-16','end':'2026-10-16','fields':['unknown']}
        with self.assertRaises(ValueError):EastmoneyDaily({}).collect(request)
        request['fields']=['close']
        for data in [{'code':'920001','klines':['2026-10-16,10,11,12,9,23,24000,30,10,1,2']},
                     {'code':'920000','klines':['2026-10-16,10,nan,12,9,23,24000,30,10,1,2']}]:
            with self.assertRaises(CollectionError):
                EastmoneyDaily({},transport=lambda url:{'rc':0,'data':data}).collect(request)

    def test_daily_dispatches_provider_and_offsets_raw_pages(self):
        from collect.methods import BaostockDaily, EastmoneyDaily
        from datetime import date
        from types import SimpleNamespace
        import sys
        provider=SimpleNamespace(login=lambda:SimpleNamespace(error_code='0'),logout=lambda:None,
            query_history_k_data_plus=lambda *args,**kwargs:SimpleNamespace(error_code='0',fields=args[1].split(','),next=lambda:False))
        packet={'records':[{'code':'bj.920000','date':'2026-10-16','close':11.,'raw_page':0}],
                'raw':[{'provider':'eastmoney'}]}
        with patch.dict(sys.modules,{'baostock':provider}), \
                patch.object(BaostockDaily,'trade_calendar',return_value=([date(2026,10,16),date(2026,10,19)],{})), \
                patch.object(EastmoneyDaily,'collect',return_value=packet) as fetch:
            result=BaostockDaily({}).collect({'codes':['sh.600000','bj.920000'],
                'fields':['close'],'start':'2026-10-16','end':'2026-10-16'})
        self.assertEqual(fetch.call_args.args[0]['codes'],['bj.920000'])
        self.assertEqual(result['records'][0]['raw_page'],2)
        self.assertEqual(result['raw'][2]['provider'],'eastmoney')

    def test_bse_transport_accepts_jsonp_and_uses_post_redirect_support(self):
        from collect.methods import BseCatalogue
        from types import SimpleNamespace
        body='null('+json.dumps([self.page(0)])+')'
        response=SimpleNamespace(text=body,raise_for_status=lambda:None)
        with patch('requests.post',return_value=response) as post:
            page=BseCatalogue.page(0)
        self.assertEqual(page['totalElements'],2)
        self.assertEqual(post.call_args.kwargs['data']['page'],'0')
        self.assertEqual(post.call_args.kwargs['timeout'],30)

class EastmoneyFactorTests(unittest.TestCase):
    def request(self):
        return {'codes':['bj.920000'],'start':'2026-10-16','end':'2026-10-16',
                'fields':['peTTM','pbMRQ','psTTM','pcfNcfTTM']}

    def values(self):
        return [{'SECUCODE':'920000.BJ','SECURITY_CODE':'920000','TRADE_DATE':'2026-10-16 00:00:00',
                 'PE_TTM':20.,'PB_MRQ':2.,'PS_TTM':3.,'PCF_OCF_TTM':999.,'TOTAL_MARKET_CAP':1200.}]

    def cash(self):
        return [{'SECUCODE':'920000.BJ','SECURITY_CODE':'920000','REPORT_DATE':report,
                 'NOTICE_DATE':notice,'CCE_ADD':value} for report,notice,value in [
                     ('2026-06-30','2026-08-14',60.),('2025-12-31','2026-04-21',100.),
                     ('2025-06-30','2025-08-14',40.),('2026-09-30','2026-10-30',9000.)]]

    def transport(self, url):
        from urllib.parse import urlparse,parse_qs
        query=parse_qs(urlparse(url).query)
        rows=self.cash() if query['reportName']==['RPT_DMSK_FN_CASHFLOW'] else self.values()
        return {'success':True,'code':0,'result':{'pages':1,'count':len(rows),'data':rows}}

    def test_exact_cash_definition_and_no_future_reports(self):
        from collect.methods import EastmoneyFactors
        packet=EastmoneyFactors({},transport=self.transport).collect(self.request())
        row=packet['records'][0]
        self.assertEqual(row['pcfNcfTTM'],10.) # 1200 / (60 + 100 - 40)
        self.assertNotEqual(row['pcfNcfTTM'],999.)
        self.assertEqual(row['peTTM'],20.)
        self.assertEqual(len(packet['raw']),2)
        self.assertEqual(row['date'],'2026-10-16')

    def test_missing_prior_year_or_undisclosed_cash_is_not_backfilled(self):
        from collect.methods import EastmoneyFactors,CollectionError
        for rows in [self.cash()[:2], [{**r,'NOTICE_DATE':'2026-10-30'} for r in self.cash()]]:
            with patch.object(self,'cash',return_value=rows):
                with self.assertRaises(CollectionError):
                    EastmoneyFactors({},transport=self.transport).collect(self.request())

    def test_wrong_identity_duplicate_dates_and_bad_numbers_reject_whole_packet(self):
        from collect.methods import EastmoneyFactors,CollectionError
        for rows in [[{**self.values()[0],'SECUCODE':'920000.SZ'}],self.values()*2,
                     [{**self.values()[0],'PE_TTM':float('nan')}]]:
            with patch.object(self,'values',return_value=rows):
                with self.assertRaises(CollectionError):
                    EastmoneyFactors({},transport=self.transport).collect(self.request())

    def test_daily_merges_standard_factors_with_prices(self):
        from collect.methods import EastmoneyDaily
        def fetch(url):
            if 'kline/get' in url:return {'rc':0,'data':{'code':'920000','klines':[
                '2026-10-16,10,11,12,9,23,24000,30,10,1,2']}}
            return self.transport(url)
        packet=EastmoneyDaily({},transport=fetch).collect({**self.request(),
            'fields':['open','high','low','close','volume','amount','peTTM','pbMRQ','psTTM','pcfNcfTTM']})
        self.assertEqual(packet['records'][0]['pcfNcfTTM'],10.)
        self.assertEqual(packet['records'][0]['volume'],2300.)
        self.assertEqual({p['provider'] for p in packet['raw']},{'eastmoney'})

    def test_daily_trading_status_requires_actual_trades(self):
        from collect.methods import EastmoneyDaily,CollectionError
        request={'codes':['bj.920000'],'start':'2026-10-16','end':'2026-10-16','fields':['close','tradestatus']}
        response={'rc':0,'data':{'code':'920000','klines':['2026-10-16,10,11,12,9,23,24000,30,10,1,2']}}
        packet=EastmoneyDaily({},transport=lambda url:response).collect(request)
        self.assertEqual(packet['records'][0]['tradestatus'],1.)
        response['data']['klines']=['2026-10-16,10,11,12,9,0,0,30,10,1,0']
        with self.assertRaises(CollectionError):EastmoneyDaily({},transport=lambda url:response).collect(request)

    def test_missing_factor_date_is_not_silently_skipped(self):
        from collect.methods import EastmoneyDaily,CollectionError
        def fetch(url):
            if 'kline/get' in url:return {'rc':0,'data':{'code':'920000','klines':[
                '2026-10-15,10,11,12,9,23,24000,30,10,1,2']}}
            return self.transport(url)
        with self.assertRaises(CollectionError):
            EastmoneyDaily({},transport=fetch).collect({**self.request(),'start':'2026-10-15',
                'fields':['close','peTTM']})

class HistoricalFillTests(unittest.TestCase):
    def test_partial_calendar_cannot_shorten_requested_history(self):
        from collect.methods import Methods
        request={'codes':['sh.600000'],'start':'2003-01-01','end':'2026-10-17'}
        coverage={'calendar_complete':False,'missing':{'sh.600000':['2014-01-02']}}
        self.assertEqual(Methods.missing_request(request,coverage),request)


    def test_old_and_new_years_read_with_different_optional_columns(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from datetime import date
        from types import SimpleNamespace
        from collect.methods import Methods
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            row={'code':'sh.600000','date':'2003-01-02','open':10.,'high':11.,'low':9.,
                 'close':10.,'volume':100.,'amount':1000.}
            pq.write_table(pa.Table.from_pylist([{**row,'name':'浦发银行'}]),root/'old.parquet')
            pq.write_table(pa.Table.from_pylist([{**row,'date':'2003-01-03','peTTM':10.}]),root/'new.parquet')
            data=SimpleNamespace(transaction=lambda *a:contextlib.nullcontext(),path=lambda kind,identity,name:root/name)
            methods=Methods(data)
            context=({},[date(2003,1,2),date(2003,1,3),date(2003,1,6)],{},('sh.600000',),
                     [('cn_a','old.parquet'),('cn_a','new.parquet')],'snapshot',('sh.600000',))
            with patch.object(methods,'_daily_view_context',return_value=context):
                page=methods.daily_collection_page({'id':'daily'},'cn_a')
            self.assertEqual(page['total'],2)
            self.assertIsNone(page['records'][1]['name'])
            self.assertEqual(page['records'][1]['peTTM'],10.)

class HaltSourceTests(unittest.TestCase):
    def fetch(self, status):
        from collect.methods import BaostockDaily
        from types import SimpleNamespace
        from datetime import date
        import sys
        values={'date':'2009-12-29','code':'sh.600001','open':'10','high':'10','low':'10','close':'10',
                'volume':'','amount':'','tradestatus':status}
        def query(code, fields, **kwargs):
            names=fields.split(',');row=[values[n] for n in names]
            return SimpleNamespace(error_code='0',fields=names,next=iter([True,False]).__next__,get_row_data=lambda:row)
        provider=SimpleNamespace(login=lambda:SimpleNamespace(error_code='0'),logout=lambda:None,
                                 query_history_k_data_plus=query)
        with patch.dict(sys.modules,{'baostock':provider}),patch.object(BaostockDaily,'trade_calendar',
                return_value=([date(2009,12,29),date(2009,12,30)],{})):
            return BaostockDaily({}).collect({'codes':['sh.600001'],'start':'2009-12-29','end':'2009-12-29',
                'fields':['open','high','low','close','volume','amount','tradestatus']})

    def test_explicit_halt_has_zero_traded_quantity_without_changing_raw(self):
        packet=self.fetch('0')
        self.assertEqual(packet['records'][0]['volume'],0.)
        self.assertEqual(packet['records'][0]['amount'],0.)
        self.assertEqual(packet['raw'][1]['response']['rows'][0][-3:-1],['',''])

    def test_unknown_traded_quantity_is_not_filled_on_a_trading_day(self):
        from collect.methods import CollectionError
        with self.assertRaises(CollectionError):self.fetch('1')

class BridgeBatchTests(unittest.TestCase):
    def test_progress_and_next_request_in_one_pipe_write_are_both_consumed(self):
        import subprocess,sys,selectors
        from dataclasses import asdict
        from types import SimpleNamespace
        pending=ui.UiState(targets={'cli':ui.UiSlot(operations={'cli.collect':ui.UiOperationState(status='pending')})})
        done=ui.UiState(targets={'cli':ui.UiSlot(operations={'cli.collect':ui.UiOperationState(status='succeeded')})})
        request=ui.UiRequest('nested','collection.describe','urn:lianghua:ui:input:collection.describe:v1',{})
        burst='\n'.join(json.dumps(f) for f in [
            {'kind':'state','state':asdict(pending)}, {'kind':'request','request':request.to_dict()}])+'\n'
        final=json.dumps({'kind':'state','state':asdict(done)})+'\n'
        script='import os,sys\nsys.stdin.readline()\nos.write(1,'+repr(burst.encode())+')\nsys.stdin.readline()\nos.write(1,'+repr(final.encode())+')\nsys.stdin.readline()\n'
        real_popen=subprocess.Popen;real_selector=selectors.DefaultSelector
        seen=[]
        async def send(request):seen.append(request);return []
        backend=SimpleNamespace(send=send,close=lambda:None)
        class Ready:
            def __init__(self):self.inner=real_selector()
            def __enter__(self):return self
            def __exit__(self,*args):self.inner.close()
            def register(self,*args):return self.inner.register(*args)
            def select(self,timeout):
                events=self.inner.select(min(timeout,1))
                if not events:raise RuntimeError('buffered frame was not consumed')
                return events
        def popen(*args,**kwargs):return real_popen([sys.executable,'-c',script],**kwargs)
        selectors_created=[]
        def selector():
            selectors_created.append(True)
            return Ready() if len(selectors_created)==1 else real_selector()
        with patch('backend.build_backend',return_value=backend),patch.object(ui.subprocess,'Popen',side_effect=popen), \
                patch.object(ui.selectors,'DefaultSelector',side_effect=selector):
            state=ui.exchange(ui.UiAction('batch','invoke','cli',{'selector':'TEST:[2003,2012]'},'cli.collect'),on_state=lambda s:None)
        self.assertEqual(len(seen),1)
        self.assertEqual(state.targets['cli'].operations['cli.collect'].status,'succeeded')
