"""The only composition port for local strategy files and parameter schemas."""
from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from hashlib import sha256
import importlib
import inspect
import json
from math import isfinite
from pathlib import Path
import pkgutil
from typing import get_type_hints

from core.contracts import DataRequest

PACKAGE = 'plugins.strategies'


def canonical_hash(value) -> str:
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def module_spec(name: str):
    if not isinstance(name,str) or not name.startswith(PACKAGE+'.') or '.' in name[len(PACKAGE)+1:]:
        raise ValueError('strategy must name one local strategy file')
    package=importlib.import_module(PACKAGE)
    file=name[len(PACKAGE)+1:]+'.py'
    if not any((Path(folder)/file).parent==Path(folder) and (Path(folder)/file).is_file()
               for folder in package.__path__):
        raise ValueError('策略文件已移除或不存在：'+name)
    module=importlib.import_module(name)
    if not hasattr(module,'Params') or not is_dataclass(module.Params) or not hasattr(module,'Strategy'):
        raise ValueError('strategy file must export Params and Strategy')
    if not isinstance(getattr(module,'NAME',None),str) or not module.NAME.strip():
        raise ValueError('strategy file must export a nonempty NAME')
    if not module.Params.__dataclass_params__.frozen:
        raise ValueError('Params must be frozen')
    return module


def validate_params(params):
    types=get_type_hints(type(params))
    for item in fields(params):
        value=getattr(params,item.name); kind=types[item.name]; meta=item.metadata
        valid=(type(value) is int if kind is int else type(value) in (int,float) if kind is float else type(value) is kind)
        if not valid or (kind in (int,float) and not isfinite(value)):
            raise ValueError(f'{item.name} must be a finite {kind.__name__}')
        if 'minimum' in meta and value<meta['minimum']: raise ValueError(f'{item.name} is below minimum')
        if 'maximum' in meta and value>meta['maximum']: raise ValueError(f'{item.name} is above maximum')
        if 'exclusive_minimum' in meta and value<=meta['exclusive_minimum']: raise ValueError(f'{item.name} must exceed minimum')
        if 'options' in meta and value not in meta['options']: raise ValueError(f'{item.name} is not an allowed option')


def load_strategy(name: str, values: dict):
    if not isinstance(values,dict): raise ValueError('params must be an object')
    module=module_spec(name)
    declared={item.name:item for item in fields(module.Params)}
    unknown=set(values)-set(declared)
    if unknown: raise ValueError('unknown parameters: '+', '.join(sorted(unknown)))
    params=module.Params(**values)
    validate_params(params)
    strategy=module.Strategy(params)
    if not isinstance(strategy.strategy_id,str) or not strategy.strategy_id or not callable(strategy.generate):
        raise ValueError('strategy interface invalid')
    if not isinstance(strategy.requirements(),DataRequest): raise ValueError('requirements must return DataRequest')
    return strategy


def catalog() -> list[dict]:
    package=importlib.import_module(PACKAGE)
    items=[]
    for entry in sorted(pkgutil.iter_modules(package.__path__),key=lambda item:item.name):
        if entry.ispkg or entry.name.startswith('_'): continue
        module=module_spec(PACKAGE+'.'+entry.name)
        strategy=load_strategy(module.__name__,{})
        kinds={int:'integer',float:'number',str:'string',bool:'boolean'}
        types=get_type_hints(module.Params)
        parameters=[{'key':item.name,'type':kinds[types[item.name]],'default':getattr(strategy.params,item.name),**dict(item.metadata)} for item in fields(module.Params)]
        items.append({'module':module.__name__,'strategy_id':strategy.strategy_id,'name':module.NAME,
                      'description':module.__doc__,'parameters':parameters,'defaults':asdict(strategy.params)})
    return items


def code_hash() -> str:
    """Cover all executable package code, including shared indicator/ledger rules."""
    root=Path(__file__).parent
    hashes={str(path.relative_to(root)):sha256(path.read_bytes()).hexdigest() for path in sorted(root.rglob('*.py'))}
    return canonical_hash(hashes)
