"""HTTP business contract, independent of provider schemas."""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

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
    initial_cash: str
    start: str
    end: str
    dataset_id: str
    @model_validator(mode='after')
    def period(self):
        if date.fromisoformat(self.start)>date.fromisoformat(self.end):
            raise ValueError('start must not exceed end')
        try: cash=Decimal(self.initial_cash)
        except InvalidOperation as exc: raise ValueError('initial_cash must be a decimal string') from exc
        if not cash.is_finite() or cash<=0: raise ValueError('initial_cash must be positive and finite')
        return self
