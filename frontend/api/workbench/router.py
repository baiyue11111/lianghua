"""Composition root: replace adapters without changing routes or consumers."""
import importlib
import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from .store import Store, SyncService
from .schedule import Scheduler
from datetime import date
from .benchmark import Benchmarks
from .backtests import Backtests, BacktestBlocked
from .models import BacktestRequest
from .models import BenchmarkSpec,BenchmarkSeries,SyncRequest, ReviewRequest, Job, SnapshotPage, Review, ScheduleRequest, RunList, RunDetail, SourceList, Evidence, Schedule


def factory(name):
    module,symbol=name.split(':')
    return getattr(importlib.import_module(module),symbol)

def create_router(root:Path,config:dict,providers=None,research=None):
    router=APIRouter(prefix='/api/workbench',tags=['workbench'])
    root=root.resolve(); storage=(root/config['storage']).resolve()
    if not storage.is_relative_to(root): raise ValueError('storage escapes project')
    store=Store(storage)
    sources={s['id']:s for s in config['sources']}
    if len(sources)!=len(config['sources']): raise ValueError('duplicate source identity')
    if any(not s.replace('_','').isalnum() for s in sources): raise ValueError('invalid source identity')
    providers=providers if providers is not None else {key:factory(s['adapter'])(s) for key,s in sources.items()}
    service=SyncService(store,providers)
    scheduler=Scheduler(service)
    benchmarks=Benchmarks(store,config.get('benchmarks',[]))
    router.add_event_handler("startup",scheduler.start)
    router.add_event_handler("shutdown",scheduler.stop.set)
    research=research if research is not None else factory(config['research']['adapter'])(root,config['research'])
    backtests=Backtests(root,config.get('backtests',{}),store)
    router.store=store; router.service=service
    @router.get('/strategies')
    def strategies(): return backtests.catalog()
    @router.post('/backtests/preflight')
    def preflight(request:BacktestRequest):
        try:return backtests.preflight(request.model_dump())
        except (ValueError,TypeError,ModuleNotFoundError) as exc:
            raise HTTPException(422,{'code':'invalid_parameters','message':str(exc)})
    @router.post('/backtests')
    def backtest(request:BacktestRequest):
        try:return backtests.run(request.model_dump())
        except BacktestBlocked as exc:
            raise HTTPException(409,{'code':'backtest_blocked','message':str(exc),'issues':exc.preflight['issues'],'preflight':exc.preflight})
        except (ValueError,TypeError,ModuleNotFoundError) as exc:
            raise HTTPException(422,{'code':'invalid_parameters','message':str(exc)})

    @router.get('/benchmarks',response_model=list[BenchmarkSpec])
    def benchmark_list():return benchmarks.list()
    @router.get('/benchmarks/{identity}',response_model=BenchmarkSeries)
    def benchmark(identity:str,start:date,end:date):
        try:return benchmarks.series(identity,start.isoformat(),end.isoformat())
        except KeyError:raise HTTPException(404,'benchmark not found')
        except ValueError as exc:raise HTTPException(409,str(exc))
    @router.get('/runs',response_model=RunList)
    def runs(): return {'items':research.list()}
    @router.get('/runs/{run_id}',response_model=RunDetail)
    def run(run_id:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
        try: return research.detail(run_id,offset,limit)
        except KeyError: raise HTTPException(404,'run not found')
    @router.get('/sources',response_model=SourceList)
    def catalog():
        result=[]; today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
        for key,s in sources.items():
            pointer=store.read('source_jobs',key); job=store.read('jobs',pointer['job_id']) if pointer else None
            error=None; snapshot=None
            try: snapshot=store.latest(key)
            except (ValueError,KeyError) as exc: error=str(exc)
            defaults=dict(s['defaults']); days=defaults.pop('lookback_days',1)
            defaults.update(start=(today-timedelta(days=days)).isoformat(),end=today.isoformat())
            result.append({'id':key,'name':s['name'],'frequency':s['frequency'],'defaults':defaults,
                           'job':job,'schedule':store.read('schedules',key),'integrity_error':error,
                           'snapshot':None if snapshot is None else {k:snapshot[k] for k in ('snapshot_id','collected_at','data_as_of','scope')},
                           'records':len(snapshot['records']) if snapshot else 0})
        return {'items':result}
    @router.post('/sources/{source_id}/sync',response_model=Job,status_code=202)
    def sync(source_id:str,request:SyncRequest):
        try: job=service.begin(source_id,request.model_dump(mode='json'))
        except KeyError: raise HTTPException(404,'source not found')
        except RuntimeError as exc: raise HTTPException(409,str(exc))
        threading.Thread(target=service.execute,args=(job.copy(),),daemon=True).start()
        return job
    @router.post('/sources/{source_id}/schedule',response_model=Schedule)
    def schedule(source_id:str,request:ScheduleRequest):
        try: return scheduler.configure(source_id,request.enabled,request.interval_seconds,request.request.model_dump(mode='json'))
        except KeyError: raise HTTPException(404,'source not found')
    @router.get('/jobs/{job_id}',response_model=Job)
    def job(job_id:str):
        if not job_id.isalnum(): raise HTTPException(404,'job not found')
        value=store.read('jobs',job_id)
        if not value: raise HTTPException(404,'job not found')
        return value
    @router.get('/snapshots/{snapshot_id}',response_model=SnapshotPage)
    def snapshot(snapshot_id:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
        try: value=store.get_snapshot(snapshot_id)
        except KeyError: raise HTTPException(404,'snapshot not found')
        except ValueError as exc: raise HTTPException(409,str(exc))
        return {**{k:v for k,v in value.items() if k!='raw'},'records':value['records'][offset:offset+limit],
                'offset':offset,'limit':limit,'total':len(value['records'])}
    @router.get('/snapshots/{snapshot_id}/records/{index}',response_model=Evidence)
    def evidence(snapshot_id:str,index:int):
        try: value=store.get_snapshot(snapshot_id)
        except KeyError: raise HTTPException(404,'snapshot not found')
        except ValueError as exc: raise HTTPException(409,str(exc))
        if not 0<=index<len(value['records']): raise HTTPException(404,'record not found')
        return {'snapshot_id':snapshot_id,'collected_at':value['collected_at'],'request':value['request'],
                'record_index':index,'record':value['records'][index],'raw':value['raw'],
                'scope':value['scope'],'reviews':[r for r in store.reviews() if r['snapshot_id']==snapshot_id and r['record_index']==index]}
    @router.get('/jobs/{job_id}/evidence')
    def failed_evidence(job_id:str):
        if not job_id.isalnum(): raise HTTPException(404,'job not found')
        value=store.read('failed_evidence',job_id)
        if not value: raise HTTPException(404,'failed raw evidence not present')
        return value
    @router.get('/reviews',response_model=list[Review])
    def reviews(): return store.reviews()
    @router.post('/reviews',response_model=Review)
    def review(request:ReviewRequest):
        try: return store.review(**request.model_dump())
        except KeyError: raise HTTPException(404,'snapshot not found')
        except ValueError as exc: raise HTTPException(409,str(exc))
    return router
