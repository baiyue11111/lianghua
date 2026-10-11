"""HTTP business contract, independent of provider schemas."""
from datetime import date
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

class SyncRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)
    start: date
    end: date
    keyword: str = ''
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

class Run(BaseModel):
    id:str; run_id:str; strategy_id:str; status:str; qualification:Literal['unverified']
    limitations:list[str]; metrics:dict[str,float|int|None]; manifest_file:str
    manifest_sha256:str; metrics_sha256:str; source_hashes:dict[str,str]
    config_hash:str|None; note:str
    parameters:dict[str,Any]=Field(default_factory=dict)
    effective_config:dict[str,Any]=Field(default_factory=dict)
    code_hash:str|None=None; data_hash:str|None=None; execution_hash:str|None=None
class RunList(BaseModel):
    items:list[Run]
class EvidenceFile(BaseModel):
    file:str; sha256:str
class EquityPoint(BaseModel):
    at:str; equity:float
class RunDetail(Run):
    manifest:dict[str,Any]; trades:list[dict[str,Any]]; trades_total:int=0
    equity:list[EquityPoint]; equity_sampling:dict[str,int]|None=None
    evidence:list[EvidenceFile]; offset:int; limit:int
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

class BenchmarkSpec(BaseModel):
    id:str;name:str;source_id:str;code:str;provider:str;currency:str;return_type:str
class BenchmarkPoint(BaseModel):
    at:str;close:float
class BenchmarkSeries(BenchmarkSpec):
    snapshot_id:str;collected_at:str;start:str;end:str;points:list[BenchmarkPoint]

class BacktestRequest(BaseModel):
    model_config = {'extra':'forbid','strict':True}
    module: str
    params: dict[str,Any]
    initial_cash: float = Field(gt=0,allow_inf_nan=False)
    start: str
    end: str
    dataset_id: str
    @model_validator(mode='after')
    def period(self):
        if date.fromisoformat(self.start)>date.fromisoformat(self.end):
            raise ValueError('start must not exceed end')
        return self
