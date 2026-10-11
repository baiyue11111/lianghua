"""Immutable, complete strategy-run configuration; defaults resolved once."""
from dataclasses import asdict, dataclass
from datetime import date
import json
from math import isfinite
from typing import Mapping
from strategy import canonical_hash, load_strategy


@dataclass(frozen=True)
class RunConfig:
    payload: str
    config_hash: str

    @property
    def values(self): return json.loads(self.payload)

    @property
    def initial_cash(self): return self.values['initial_cash']

    @classmethod
    def from_mapping(cls,raw:Mapping):
        required={'module','params','initial_cash','start','end','dataset_id'}
        allowed=required|{'requirements','state_start','price_basis','execution','data_hash','account_hash','data_scope'}
        if set(raw)-allowed: raise ValueError('unknown run fields: '+', '.join(sorted(set(raw)-allowed)))
        missing=required-set(raw)
        if missing: raise ValueError('missing run fields: '+', '.join(sorted(missing)))
        cash=raw['initial_cash']
        if type(cash) not in (int,float) or not isfinite(cash) or cash<=0:
            raise ValueError('initial_cash must be a positive finite number')
        if any(type(raw[key]) is not str for key in ('module','start','end','dataset_id')):
            raise ValueError('module, interval and dataset identity must be strings')
        if date.fromisoformat(raw['start'])>date.fromisoformat(raw['end']):
            raise ValueError('start must not exceed end')
        strategy=load_strategy(raw['module'],raw['params'])
        values={**raw,'initial_cash':float(cash),'params':asdict(strategy.params),
                'requirements':asdict(strategy.requirements())}
        payload=json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        return cls(payload,canonical_hash(values))
