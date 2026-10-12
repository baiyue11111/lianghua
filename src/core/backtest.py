"""Transport-independent input, output and errors for the backtest use case.

These are public boundary data, not plug-in objects or per-step audit records.
Adapters own serialization of datetimes and exact Decimal amounts.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from types import MappingProxyType

from .contracts import Scalar, _aware, _decimal, _name


class BacktestError(ValueError):
    """Stable machine-readable code; the original exception remains the cause."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BacktestRequest:
    run_id: str
    strategy_id: str
    parameters: Mapping[str, Scalar]
    dataset_id: str
    start: datetime
    end: datetime
    initial_cash: Decimal
    fields: tuple[str, ...] = ()
    history: int = 0
    price_basis: str = "adjusted"
    as_of: date | None = None
    symbols: tuple[str, ...] | None = None
    universe: str = "dataset"
    settlement_parameters: Mapping[str, object] = None
    evaluation_parameters: Mapping[str, object] = None
    benchmark_id: str | None = None

    def __post_init__(self) -> None:
        try:
            for name in ('run_id', 'strategy_id', 'dataset_id'):
                _name(getattr(self, name), name)
            _aware(self.start, 'start')
            _aware(self.end, 'end')
            if self.start > self.end:
                raise ValueError('start must not exceed end')
            _decimal(self.initial_cash, 'initial_cash', positive=True)
            fields = tuple(self.fields)
            if fields and (len(set(fields)) != len(fields)
                    or any(not isinstance(field, str) or not field.strip() for field in fields)):
                raise ValueError('fields must be unique non-empty strings')
            object.__setattr__(self, 'fields', fields)
            if type(self.history) is not int or self.history < 0:
                raise ValueError('history must be a non-negative integer')
            if self.price_basis != 'adjusted':
                raise ValueError('unsupported price basis')
            if self.as_of is not None and not isinstance(self.as_of, date):
                raise ValueError('as_of must be a date')
            symbols = None if self.symbols is None else tuple(self.symbols)
            if symbols is not None and (not symbols or len(set(symbols)) != len(symbols)
                    or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)):
                raise ValueError('symbols must be ordered and unique')
            object.__setattr__(self, 'symbols', symbols)
            _name(self.universe, 'universe')
            if self.benchmark_id is not None:
                _name(self.benchmark_id, 'benchmark_id')
            if self.settlement_parameters is None:
                object.__setattr__(self, 'settlement_parameters', MappingProxyType({}))
            elif not isinstance(self.settlement_parameters, Mapping):
                raise TypeError('settlement parameters must be a mapping')
            else:
                object.__setattr__(self, 'settlement_parameters', MappingProxyType(dict(self.settlement_parameters)))
            if self.evaluation_parameters is None:
                object.__setattr__(self, 'evaluation_parameters', MappingProxyType({}))
            elif not isinstance(self.evaluation_parameters, Mapping):
                raise TypeError('evaluation parameters must be a mapping')
            else:
                evaluation = dict(self.evaluation_parameters)
                if set(evaluation) - {'periods', 'baseline'}:
                    raise ValueError('unsupported evaluation parameters')
                if 'periods' in evaluation and (type(evaluation['periods']) is not int
                                                or evaluation['periods'] < 1):
                    raise ValueError('evaluation periods must be a positive integer')
                if 'baseline' in evaluation and (type(evaluation['baseline']) not in (int, float)
                                                  or not isfinite(evaluation['baseline'])):
                    raise ValueError('evaluation baseline must be a finite number')
                object.__setattr__(self, 'evaluation_parameters', MappingProxyType(evaluation))
            if not isinstance(self.parameters, Mapping):
                raise TypeError('parameters must be a mapping')
            params = dict(self.parameters)
            for key, value in params.items():
                _name(key, 'parameter name')
                if type(value) not in (str, int, float, bool, type(None)):
                    raise TypeError('parameter values must be scalars')
                if type(value) is float and not isfinite(value):
                    raise ValueError('parameter numbers must be finite')
            object.__setattr__(self, 'parameters', MappingProxyType(params))
        except (TypeError, ValueError) as error:
            raise BacktestError('invalid_request', str(error)) from error


@dataclass(frozen=True)
class BacktestResponse:
    run_id: str
    strategy_id: str
    dataset_id: str
    first_at: datetime
    last_at: datetime
    steps: int
    fills: int
    final_cash: Decimal
    final_equity: Decimal
    positions: Mapping[str, Decimal]
    pending_orders: int
    account_result: object | None = None
    dataset: object | None = None
    metrics: Mapping[str, float | int | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'positions', MappingProxyType(dict(self.positions)))
        if not isinstance(self.metrics, Mapping):
            raise TypeError('metrics must be a mapping')
        metrics = dict(self.metrics)
        for key, value in metrics.items():
            if not isinstance(key, str):
                raise TypeError('metric keys must be strings')
            if value is not None:
                if type(value) not in (int, float) or not isfinite(value):
                    raise ValueError('metric values must be finite numbers or None')
        object.__setattr__(self, 'metrics', MappingProxyType(metrics))


@dataclass(frozen=True)
class Trial:
    request: BacktestRequest
    response: BacktestResponse
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.request, BacktestRequest):
            raise TypeError('trial request must be BacktestRequest')
        if not isinstance(self.response, BacktestResponse):
            raise TypeError('trial response must be BacktestResponse')
        if type(self.score) not in (int, float) or not isfinite(self.score):
            raise ValueError('trial score must be a finite number')
        object.__setattr__(self, 'score', float(self.score))


@dataclass(frozen=True)
class SearchResponse:
    trials: tuple[Trial, ...]
    best_index: int
    validation: BacktestResponse | None = None

    def __post_init__(self) -> None:
        trials = tuple(self.trials)
        if not trials:
            raise ValueError('search requires at least one trial')
        if type(self.best_index) is not int or not 0 <= self.best_index < len(trials):
            raise ValueError('best_index must point to a trial')
        if any(not isinstance(trial, Trial) for trial in trials):
            raise TypeError('trials must contain Trial values')
        if self.validation is not None and not isinstance(self.validation, BacktestResponse):
            raise TypeError('validation must be BacktestResponse or None')
        object.__setattr__(self, 'trials', trials)
