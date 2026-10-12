"""Immutable source evidence, mutable job pointers and explicit human reviews."""
from __future__ import annotations
import hashlib
import json
import threading
from urllib.request import Request, urlopen
from datetime import datetime, timezone


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort


def now():
    return datetime.now(timezone.utc).isoformat()

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def saved(data, kind, identity, name):
    """Absent evidence is distinct from corrupt or unreadable evidence."""
    from protocal import DataError
    try:
        return data.read(kind, identity, name)
    except DataError as error:
        if error.code != 'missing':
            raise
        return None


class Store:
    def __init__(self, data: DataPort, clock=now):
        self.data = data
        self.clock = clock
        self.lock = threading.RLock()
    @staticmethod
    def _kind(group):
        return 'sources' if group in {'snapshots','failed_evidence'} else 'state'
    def write(self, group, key, value):
        self.data.write(self._kind(group),group,f'{key}.json',encoded(value))
    def read(self, group, key):
        from protocal import DataError
        try:
            raw = self.data.read(self._kind(group), group, f'{key}.json')
        except DataError as error:
            if error.code != 'missing':
                raise
            return None
        return json.loads(raw)
    def items(self, group):
        kind=self._kind(group)
        return [json.loads(self.data.read(kind,group,ref.name))
                for ref in self.data.list(kind, group) if ref.identity==group and ref.name.endswith('.json')]
    def snapshot(self, source_id, result, request):
        value={'source_id':source_id,'collected_at':self.clock(),'request':request,**result}
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
        return self.items('reviews')
    def review(self,snapshot_id,record_index,status,note):
        index=record_index
        snap=self.get_snapshot(snapshot_id)
        if not 0<=index<len(snap['records']): raise ValueError('record index outside snapshot')
        if status not in {'correct','pending','anomaly'}: raise ValueError('invalid review status')
        value={'snapshot_id':snapshot_id,'record_index':index,'status':status,'note':note,'reviewed_at':self.clock()}
        self.write('reviews',f'{snapshot_id}-{index}',value)
        return value


def http(url, data=None):
    req=Request(url,data=data,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/',
                                      'Content-Type':'application/x-www-form-urlencoded'})
    with urlopen(req,timeout=25) as response: return json.loads(response.read())


class CollectionError(RuntimeError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence
