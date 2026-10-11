"""Explicit periodic collection, independent of provider choice and UI lifetime."""
from datetime import date, datetime, timedelta
import threading
import time
from zoneinfo import ZoneInfo
from .store import now

class Scheduler:
    def __init__(self,service):
        self.service=service; self.store=service.store; self.stop=threading.Event()
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
                    (dispatch or start)(job)
                value['next_at']=at+value['interval_seconds']; self.store.write('schedules',source_id,value)
    def start(self):
        def loop():
            while not self.stop.wait(1): self.tick()
        threading.Thread(target=loop,daemon=True).start()
