"""Transport-independent input, output and errors for the backtest use case.

These are public boundary data, not plug-in objects or per-step audit records.
Adapters own serialization of datetimes and exact Decimal amounts.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
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

    def __post_init__(self) -> None:
        try:
            for name in ('run_id', 'strategy_id', 'dataset_id'):
                _name(getattr(self, name), name)
            _aware(self.start, 'start')
            _aware(self.end, 'end')
            if self.start > self.end:
                raise ValueError('start must not exceed end')
            _decimal(self.initial_cash, 'initial_cash', positive=True)
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

    def __post_init__(self) -> None:
        object.__setattr__(self, 'positions', MappingProxyType(dict(self.positions)))
