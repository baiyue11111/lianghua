"""Collection inputs, task state and scheduling; no provider or file I/O."""
from __future__ import annotations
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

class SyncRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    codes: list[str] = Field(default_factory=list)
    start: date
    end: date
    keyword: str = ''
    frequency: Literal['d','w','m','5','15','30','60'] = 'd'
    adjustment: Literal['1','2','3'] = '3'
    fields: list[str] | None = None
    @model_validator(mode='after')
    def period(self):
        if self.start>self.end: raise ValueError('start must not exceed end')
        if len(self.codes)!=len(set(self.codes)): raise ValueError('duplicate codes')
        return self
class ReviewRequest(BaseModel):
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    record_index: int = Field(ge=0)
    status: Literal['correct','pending','anomaly']
    note: str = Field(max_length=4000)
class Job(BaseModel):
    job_id:str; source_id:str; request:dict[str,Any]
    status:Literal['running','succeeded','failed']
    started_at:str; finished_at:str|None; error:str|None
    snapshot_id:str|None; records:int
class SnapshotPage(BaseModel):
    snapshot_id:str; source_id:str; collected_at:str; request:dict[str,Any]
    data_as_of:str|None; scope:dict[str,Any]
    records:list[dict[str,Any]]; offset:int; limit:int; total:int
class Review(BaseModel):
    snapshot_id:str; record_index:int; status:Literal['correct','pending','anomaly']; note:str; reviewed_at:str

class ScheduleRequest(BaseModel):
    enabled:bool
    interval_seconds:int=Field(ge=30,default=60)
    request:SyncRequest

class Schedule(BaseModel):
    source_id:str; enabled:bool; interval_seconds:int
    request:SyncRequest; window_days:int; next_at:float|None; error:str|None

class SnapshotSummary(BaseModel):
    snapshot_id:str; collected_at:str; data_as_of:str|None; scope:dict[str,Any]
class Source(BaseModel):
    id:str; name:str; frequency:str; defaults:dict[str,Any]
    job:Job|None; schedule:Schedule|None; integrity_error:str|None
    snapshot:SnapshotSummary|None; records:int
class SourceList(BaseModel):
    items:list[Source]
class Evidence(BaseModel):
    snapshot_id:str;collected_at:str;request:dict[str,Any];record_index:int
    record:dict[str,Any];raw:list[dict[str,Any]];scope:dict[str,Any];reviews:list[Review]


class SyncService:
    def __init__(self,store,providers:dict):
        self.store=store; self.providers=providers; self.lock=threading.Lock(); self.active=set()
        self.clock = store.clock
        # A process exit cannot leave a persisted task looking alive forever.
        for value in store.items('jobs'):
            if value['status']=='running':
                value.update(status='failed',error='sync process interrupted before completion',finished_at=self.clock())
                store.write('jobs',value['job_id'],value)
    def begin(self, source_id, request):
        if source_id not in self.providers: raise KeyError(source_id)
        with self.lock:
            if source_id in self.active: raise RuntimeError('source sync already running')
            self.active.add(source_id)
        job={'job_id':uuid.uuid4().hex,'source_id':source_id,'request':request,'status':'running',
             'started_at':self.clock(),'finished_at':None,'error':None,'snapshot_id':None,'records':0}
        try:
            self.store.write('jobs',job['job_id'],job)
            self.store.write('source_jobs',source_id,{'job_id':job['job_id']})
        except Exception:
            with self.lock: self.active.remove(source_id)
            raise
        return job
    def execute(self,job):
        try:
            result=self.providers[job['source_id']].collect(job['request'])
            if not isinstance(result.get('records'),list) or not isinstance(result.get('raw'),list):
                raise ValueError('adapter must return records and raw evidence')
            snap=self.store.snapshot(job['source_id'],result,job['request'])
            job.update(status='succeeded',snapshot_id=snap['snapshot_id'],records=len(snap['records']))
        except Exception as exc:
            job.update(status='failed',error=f'{type(exc).__name__}: {exc}')
            payload={'job_id':job['job_id'],'request':job['request'],
                     'raw':getattr(exc, 'evidence', []),'error':job['error']}
            self.store.write('failed_evidence',job['job_id'],payload)
        finally:
            try:
                job['finished_at']=self.clock()
                self.store.write('jobs',job['job_id'],job)
            finally:
                with self.lock: self.active.remove(job['source_id'])
        return job
    def run(self,source_id,request):
        return self.execute(self.begin(source_id,request))

class Scheduler:
    def __init__(self,service,dispatch=None):
        self.service=service; self.store=service.store; self.stop=threading.Event(); self._thread=None
        self.dispatch = dispatch
    def configure(self,source_id,enabled,interval_seconds,request,at=None):
        if source_id not in self.service.providers: raise KeyError(source_id)
        if interval_seconds<30: raise ValueError('interval must be at least 30 seconds')
        at=time.time() if at is None else at
        days=(date.fromisoformat(request['end'])-date.fromisoformat(request['start'])).days if enabled else 0
        value={'source_id':source_id,'enabled':enabled,'interval_seconds':interval_seconds,'request':request,
               'window_days':days,'next_at':at+interval_seconds if enabled else None,'error':None}
        with self.store.lock: self.store.write('schedules',source_id,value)
        return value
    def tick(self,at=None,local_date=None,dispatch=None):
        if self.stop.is_set(): return
        at=time.time() if at is None else at
        today=date.fromisoformat(local_date) if local_date else datetime.now(ZoneInfo('Asia/Shanghai')).date()
        def start(job): threading.Thread(target=self.service.execute,args=(job,),daemon=True).start()
        for source_id in self.service.providers:
            with self.store.lock:
                value=self.store.read('schedules',source_id)
                if not value or not value['enabled'] or value['next_at']>at: continue
                request={**value['request'],'start':(today-timedelta(days=value['window_days'])).isoformat(),'end':today.isoformat()}
                try:
                    job=self.service.begin(source_id,request)
                except RuntimeError as exc: value['error']=str(exc)
                else:
                    value['error']=None
                    (dispatch or self.dispatch or start)(job)
                value['next_at']=at+value['interval_seconds']; self.store.write('schedules',source_id,value)
    def start(self):
        def loop():
            while not self.stop.wait(1): self.tick()
        if self._thread is not None: raise RuntimeError('scheduler already started')
        self._thread=threading.Thread(target=loop,daemon=True)
        self._thread.start()
    def close(self):
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive(): raise RuntimeError('scheduler did not stop')
