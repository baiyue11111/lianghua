import json
import tempfile
import unittest
from pathlib import Path
from frontend.api.workbench.store import Store, SyncService
from frontend.api.workbench.providers import parse_cninfo_page

class Good:
    def collect(self, request):
        return {'records': [{'code': '600000', 'date': '2026-09-30', 'close': '10.12'}],
                'raw': [{'request': request, 'response': {'data': 'original'}}],
                'data_as_of': '2026-09-30', 'scope': request}
class Bad:
    def collect(self, request):
        raise RuntimeError('provider refused')

class WorkbenchTests(unittest.TestCase):
    def test_pluggable_provider_raw_evidence_and_persisted_review(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            service = SyncService(store, {'price': Good()})
            job = service.run('price', {'codes':['600000']})
            self.assertEqual(job['status'], 'succeeded')
            snap = store.latest('price')
            self.assertEqual(snap['records'][0]['close'], '10.12')
            self.assertEqual(snap['raw'][0]['response'], {'data':'original'})
            store.review(snap['snapshot_id'], 0, 'correct', 'checked against source')
            self.assertEqual(Store(Path(d)).reviews()[0]['status'], 'correct')
            self.assertEqual(Store(Path(d)).latest('price')['snapshot_id'], snap['snapshot_id'])
            with self.assertRaises(ValueError):
                store.review(snap['snapshot_id'], 4, 'correct', '')
    def test_failure_keeps_previous_snapshot_and_exposes_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            service = SyncService(store, {'price':Good()})
            service.run('price', {})
            before=store.latest('price')
            failed=SyncService(store, {'price':Bad()}).run('price', {})
            self.assertEqual(failed['status'],'failed')
            self.assertIn('refused', failed['error'])
            self.assertEqual(store.latest('price'), before)
    def test_corrupt_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)); SyncService(store, {'p':Good()}).run('p', {})
            snap=store.latest('p'); path=Path(d)/'snapshots'/f"{snap['snapshot_id']}.json"
            data=json.loads(path.read_text()); data['records'][0]['close']='999'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'hash'): store.latest('p')
    def test_cninfo_pagination_requires_complete_shape(self):
        row={'announcementId':'a', 'orgId':'o', 'secCode':'600000', 'announcementTitle':'title',
             'announcementTime':1790812800000, 'adjunctUrl':'finalpage/a.PDF'}
        records,total,more=parse_cninfo_page({'totalRecordNum':1,'hasMore':False,'announcements':[row]},1,30)
        self.assertEqual(total,1); self.assertFalse(more)
        self.assertTrue(records[0]['url'].startswith('https://static.cninfo.com.cn/'))
        with self.assertRaises(ValueError):
            parse_cninfo_page({'totalRecordNum':2,'hasMore':False,'announcements':[row]},1,30)

class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, app, method, path, payload=None, query=b''):
        import asyncio
        body=json.dumps(payload).encode() if payload is not None else b''
        sent=[]
        async def receive(): return {'type':'http.request','body':body,'more_body':False}
        async def send(value): sent.append(value)
        scope={'type':'http','asgi':{'version':'3.0'},'method':method,'path':path,'raw_path':path.encode(),
               'query_string':query,'headers':[(b'content-type',b'application/json')],
               'scheme':'http','server':('test',80),'client':('test',1),'root_path':''}
        await app(scope,receive,send)
        status=next(v['status'] for v in sent if v['type']=='http.response.start')
        raw=b''.join(v.get('body',b'') for v in sent if v['type']=='http.response.body')
        return status,json.loads(raw)
    async def test_consumer_request_real_route_validation_and_evidence(self):
        import asyncio
        from fastapi import FastAPI
        from frontend.api.workbench.router import create_router
        class Research:
            def list(self):return []
            def detail(self,*args):raise KeyError()
        with tempfile.TemporaryDirectory() as d:
            config={'storage':'data','sources':[{'id':'p','name':'Price','frequency':'daily','defaults':{}}],'research':{}}
            router=create_router(Path(d),config,{'p':Good()},Research())
            app=FastAPI(); app.include_router(router)
            status,_=await self.call(app,'POST','/api/workbench/sources/p/sync',{'start':'2026-10-03','end':'2026-09-30'})
            self.assertEqual(status,422)
            status,job=await self.call(app,'POST','/api/workbench/sources/p/sync',{'codes':['600000'],'start':'2026-09-30','end':'2026-10-03','keyword':''})
            self.assertEqual(status,202)
            for _ in range(100):
                status,job=await self.call(app,'GET','/api/workbench/jobs/'+job['job_id'])
                if job['status']!='running':break
                await asyncio.sleep(.01)
            self.assertEqual(job['status'],'succeeded')
            status,page=await self.call(app,'GET','/api/workbench/snapshots/'+job['snapshot_id'])
            self.assertEqual(status,200);self.assertEqual(page['total'],1)
            self.assertNotIn('raw',page)
            status,evidence=await self.call(app,'GET',f"/api/workbench/snapshots/{job['snapshot_id']}/records/0")
            self.assertEqual(status,200);self.assertEqual(evidence['raw'][0]['response'],{'data':'original'})
            status,review=await self.call(app,'POST','/api/workbench/reviews',{'snapshot_id':job['snapshot_id'],'record_index':0,'status':'pending','note':'source inspected'})
            self.assertEqual(status,200); self.assertEqual(review['note'],'source inspected')
            status,reviews=await self.call(app,'GET','/api/workbench/reviews')
            self.assertEqual(len(reviews),1)
            self.assertIn('/api/workbench/sources/{source_id}/sync',app.openapi()['paths'])
    def test_source_concurrency_and_unsupported_adapter_have_no_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            service=SyncService(Store(Path(d)),{'p':Good()})
            job=service.begin('p',{})
            with self.assertRaises(RuntimeError): service.begin('p',{})
            with self.assertRaises(KeyError): service.begin('missing',{})
            service.execute(job)

class ScheduleTests(unittest.TestCase):
    def test_schedule_persists_and_rolls_query_to_current_local_date(self):
        from frontend.api.workbench.schedule import Scheduler
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)); service=SyncService(store,{'p':Good()})
            scheduler=Scheduler(service)
            scheduler.configure('p',True,60,{'codes':['x'],'start':'2026-09-25','end':'2026-10-03','keyword':''},at=100)
            self.assertTrue(Store(Path(d)).read('schedules','p')['enabled'])
            scheduler.tick(at=161,local_date='2026-10-04',dispatch=lambda job:service.execute(job))
            self.assertEqual(store.latest('p')['request']['end'],'2026-10-04')
            self.assertEqual(store.latest('p')['request']['start'],'2026-09-26')
            scheduler.configure('p',False,60,{},at=162)
            before=store.latest('p');scheduler.tick(at=300,local_date='2026-10-05')
            self.assertEqual(store.latest('p'),before)

class AnnouncementAdapterTests(unittest.TestCase):
    def test_native_pagination_and_duplicate_rejection_retain_failed_evidence(self):
        from frontend.api.workbench.providers import CninfoAnnouncements
        from urllib.parse import parse_qs
        def rows(start,count):
            return [{'announcementId':str(i),'orgId':'org','secCode':'600000',
                     'announcementTitle':f'document {i}','announcementTime':1790812800000,
                     'adjunctUrl':f'finalpage/{i}.PDF'} for i in range(start,start+count)]
        queries=[]
        def transport(url,data):
            q=parse_qs(data.decode()); queries.append(q)
            self.assertNotIn('sortName',q); self.assertNotIn('sortType',q)
            n=int(q['pageNum'][0]); return {'totalRecordNum':31,'hasMore':n==1,
                'announcements':rows(0,30) if n==1 else rows(30,1)}
        request={'start':'2026-09-30','end':'2026-09-30','keyword':''}
        provider=CninfoAnnouncements({},transport)
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)); service=SyncService(store,{'p':provider})
            good=service.run('p',request); self.assertEqual(good['status'],'succeeded')
            before=store.latest('p'); self.assertEqual(len(before['records']),31)
            self.assertEqual(before['records'][-1]['raw_page'],1)
            def duplicate(url,data):
                response=transport(url,data)
                if int(parse_qs(data.decode())['pageNum'][0])==2:response['announcements']=rows(0,1)
                return response
            provider.transport=duplicate
            failed=service.run('p',request)
            self.assertEqual(failed['status'],'failed')
            self.assertEqual(store.latest('p')['snapshot_id'],before['snapshot_id'])
            evidence=store.read('failed_evidence',failed['job_id'])
            self.assertEqual(len(evidence['raw']),2)
            self.assertIn('duplicate',evidence['error'])
