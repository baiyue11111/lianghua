import json
import tempfile
import unittest
from pathlib import Path
from protocal import build_data
from collect.data import Store
from collect.core import SyncService
from collect.announcements import parse_cninfo_page

class Good:
    def collect(self, request):
        return {'records': [{'code': '600000', 'date': '2026-09-30', 'close': '10.12'}],
                'raw': [{'request': request, 'response': {'data': 'original'}}],
                'data_as_of': '2026-09-30', 'scope': request}
class Bad:
    def collect(self, request):
        raise RuntimeError('provider refused')

class WorkbenchTests(unittest.TestCase):
    def test_store_uses_the_shared_data_port_only(self):
        from protocal import build_data

        with tempfile.TemporaryDirectory() as d:
            data = build_data(Path(d))
            store = Store(data)
            store.write("jobs", "one", {"status": "running"})
            self.assertEqual(store.read("jobs", "one"), {"status": "running"})
            self.assertTrue(data.path("state", "jobs", "one.json").is_file())
            self.assertFalse((Path(d) / "jobs").exists())

    def test_pluggable_provider_raw_evidence_and_persisted_review(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(build_data(Path(d)))
            service = SyncService(store, {'price': Good()})
            job = service.run('price', {'codes':['600000']})
            self.assertEqual(job['status'], 'succeeded')
            snap = store.latest('price')
            self.assertEqual(snap['records'][0]['close'], '10.12')
            self.assertEqual(snap['raw'][0]['response'], {'data':'original'})
            store.review(snap['snapshot_id'], 0, 'correct', 'checked against source')
            self.assertEqual(Store(build_data(Path(d))).reviews()[0]['status'], 'correct')
            self.assertEqual(Store(build_data(Path(d))).latest('price')['snapshot_id'], snap['snapshot_id'])
            with self.assertRaises(ValueError):
                store.review(snap['snapshot_id'], 4, 'correct', '')
    def test_failure_keeps_previous_snapshot_and_exposes_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(build_data(Path(d)))
            service = SyncService(store, {'price':Good()})
            service.run('price', {})
            before=store.latest('price')
            failed=SyncService(store, {'price':Bad()}).run('price', {})
            self.assertEqual(failed['status'],'failed')
            self.assertIn('refused', failed['error'])
            self.assertEqual(store.latest('price'), before)
    def test_corrupt_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            data=build_data(Path(d)); store=Store(data); SyncService(store, {'p':Good()}).run('p', {})
            snap=store.latest('p'); path=data.path('sources','snapshots',f"{snap['snapshot_id']}.json")
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

class PortContractTests(unittest.TestCase):
    def test_consumer_request_validation_and_evidence(self):
        import time
        from collect.display import CollectPort
        class Research:
            def list(self):return []
            def detail(self,*args):raise KeyError()
        with tempfile.TemporaryDirectory() as d:
            config={'storage':'data','sources':[{'id':'p','name':'Price','frequency':'daily','defaults':{}}],'research':{}}
            port=CollectPort(config['sources'],Store(build_data(Path(d))),{'p':Good()})
            with self.assertRaises(ValueError):
                port.handle('workbench.sync',{'id':'p','input':{'start':'2026-10-03','end':'2026-09-30'}})
            job=port.handle('workbench.sync',{'id':'p','input':{'codes':['600000'],'start':'2026-09-30','end':'2026-10-03','keyword':''}})
            for _ in range(100):
                job=port.handle('workbench.job',{'id':job['job_id']})
                if job['status']!='running':break
                time.sleep(.01)
            self.assertEqual(job['status'],'succeeded')
            page=port.handle('workbench.snapshot',{'id':job['snapshot_id'],'offset':0})
            self.assertEqual(page['total'],1)
            self.assertNotIn('raw',page)
            evidence=port.handle('workbench.evidence',{'id':job['snapshot_id'],'index':0})
            self.assertEqual(evidence['raw'][0]['response'],{'data':'original'})
            review=port.handle('workbench.review',{'snapshot_id':job['snapshot_id'],'record_index':0,'status':'pending','note':'source inspected'})
            self.assertEqual(review['note'],'source inspected')
            reviews=port.handle('workbench.reviews',{})
            self.assertEqual(len(reviews),1)
    def test_source_concurrency_and_unsupported_adapter_have_no_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            service=SyncService(Store(build_data(Path(d))),{'p':Good()})
            job=service.begin('p',{})
            with self.assertRaises(RuntimeError): service.begin('p',{})
            with self.assertRaises(KeyError): service.begin('missing',{})
            service.execute(job)

class ScheduleTests(unittest.TestCase):
    def test_schedule_persists_and_rolls_query_to_current_local_date(self):
        from collect.core import Scheduler
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as d:
            store=Store(build_data(Path(d))); service=SyncService(store,{'p':Good()})
            scheduler=Scheduler(service)
            scheduler.configure('p',True,60,{'codes':['x'],'start':'2026-09-25','end':'2026-10-03','keyword':''},at=100)
            self.assertTrue(Store(build_data(Path(d))).read('schedules','p')['enabled'])
            scheduler.tick(at=161,local_date='2026-10-04',dispatch=lambda job:service.execute(job))
            self.assertEqual(store.latest('p')['request']['end'],'2026-10-04')
            self.assertEqual(store.latest('p')['request']['start'],'2026-09-26')
            scheduler.configure('p',False,60,{},at=162)
            before=store.latest('p');scheduler.tick(at=300,local_date='2026-10-05')
            self.assertEqual(store.latest('p'),before)

class AnnouncementAdapterTests(unittest.TestCase):
    def test_native_pagination_and_duplicate_rejection_retain_failed_evidence(self):
        from collect.announcements import CninfoAnnouncements
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
            store=Store(build_data(Path(d))); service=SyncService(store,{'p':provider})
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
