"""Immutable, complete strategy-run configuration; defaults resolved once."""
from dataclasses import asdict, dataclass
import json
from strategy import canonical_hash


@dataclass(frozen=True)
class RunConfig:
    payload: str
    config_hash: str

    @property
    def values(self): return json.loads(self.payload)

    @property
    def initial_cash(self): return self.values['initial_cash']

    @classmethod
    def from_request(cls, request, strategy):
        values = {
            'module': request.strategy_id,
            'params': asdict(strategy.params),
            'initial_cash': float(request.initial_cash),
            'start': request.start.date().isoformat(),
            'end': request.end.date().isoformat(),
            'dataset_id': request.dataset_id,
            'fields': list(request.fields),
            'history': request.history,
            'price_basis': request.price_basis,
            'as_of': None if request.as_of is None else request.as_of.isoformat(),
            'symbols': None if request.symbols is None else list(request.symbols),
            'universe': request.universe,
            'benchmark_id': request.benchmark_id,
            'requirements': asdict(strategy.requirements()),
        }
        payload=json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        return cls(payload,canonical_hash(values))
