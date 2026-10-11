"""Immutable source evidence, mutable job pointers and explicit human reviews."""
from __future__ import annotations
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


def now():
    return datetime.now(timezone.utc).isoformat()

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

class SourceAdapter(Protocol):
    def collect(self, request: dict) -> dict: ...

class Store:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
    def write(self, group, key, value):
        path=self.root/group/f'{key}.json'; path.parent.mkdir(parents=True, exist_ok=True)
        temp=path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        temp.write_bytes(encoded(value)); os.replace(temp,path)
    def read(self, group, key):
        path=self.root/group/f'{key}.json'
        return json.loads(path.read_bytes()) if path.exists() else None
    def snapshot(self, source_id, result, request):
        value={'source_id':source_id,'collected_at':now(),'request':request,**result}
        key=digest(value); value['snapshot_id']=key
        with self.lock:
            self.write('snapshots',key,value)
            self.write('latest',source_id,{'snapshot_id':key})
        return value
    def get_snapshot(self,key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key): raise ValueError('invalid snapshot identity')
        value=self.read('snapshots',key)
        if value is None: raise KeyError(key)
        if value.get('snapshot_id')!=key or digest({k:v for k,v in value.items() if k!='snapshot_id'})!=key:
            raise ValueError('snapshot hash mismatch')
        return value
    def latest(self,source_id):
        pointer=self.read('latest',source_id)
        return self.get_snapshot(pointer['snapshot_id']) if pointer else None
    def reviews(self):
        return [json.loads(p.read_bytes()) for p in sorted((self.root/'reviews').glob('*.json'))]
    def review(self,snapshot_id,record_index,status,note):
        index=record_index
        snap=self.get_snapshot(snapshot_id)
        if not 0<=index<len(snap['records']): raise ValueError('record index outside snapshot')
        if status not in {'correct','pending','anomaly'}: raise ValueError('invalid review status')
        value={'snapshot_id':snapshot_id,'record_index':index,'status':status,'note':note,'reviewed_at':now()}
        self.write('reviews',f'{snapshot_id}-{index}',value)
        return value

class SyncService:
    def __init__(self,store:Store,providers:dict[str,SourceAdapter]):
        self.store=store; self.providers=providers; self.lock=threading.Lock(); self.active=set()
        # A process exit cannot leave a persisted task looking alive forever.
        for path in (store.root/'jobs').glob('*.json'):
            value=json.loads(path.read_bytes())
            if value['status']=='running':
                value.update(status='failed',error='sync process interrupted before completion',finished_at=now())
                store.write('jobs',value['job_id'],value)
    def begin(self, source_id, request):
        if source_id not in self.providers: raise KeyError(source_id)
        with self.lock:
            if source_id in self.active: raise RuntimeError('source sync already running')
            self.active.add(source_id)
        job={'job_id':uuid.uuid4().hex,'source_id':source_id,'request':request,'status':'running',
             'started_at':now(),'finished_at':None,'error':None,'snapshot_id':None,'records':0}
        self.store.write('jobs',job['job_id'],job)
        self.store.write('source_jobs',source_id,{'job_id':job['job_id']})
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
            if hasattr(exc,'evidence'):
                payload={'job_id':job['job_id'],'request':job['request'],'raw':exc.evidence,'error':job['error']}
                self.store.write('failed_evidence',job['job_id'],payload)
        finally:
            job['finished_at']=now(); self.store.write('jobs',job['job_id'],job)
            with self.lock: self.active.remove(job['source_id'])
        return job
    def run(self,source_id,request):
        return self.execute(self.begin(source_id,request))
