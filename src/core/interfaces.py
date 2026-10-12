"""Plugin protocols; implementations live outside the kernel."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from .backtest import Trial
from .contracts import (
    AccountSnapshot,
    ExecutionResult,
    MarketFrame,
    OrderRequest,
    DataRequest,
    StepResult,
    TargetSet,
    Scalar,
)


class DataSource(Protocol):
    def __iter__(self) -> Iterator[MarketFrame]: ...


class Strategy(Protocol):
    strategy_id: str

    def requirements(self) -> DataRequest: ...

    def generate(self, frame: MarketFrame, account: AccountSnapshot) -> TargetSet | None: ...


class RiskModel(Protocol):
    def apply(self, target: TargetSet, account: AccountSnapshot) -> TargetSet: ...


class MarketAdapter(Protocol):
    def plan(
        self,
        target: TargetSet,
        frame: MarketFrame,
        account: AccountSnapshot,
        pending: tuple[OrderRequest, ...],
    ) -> tuple[OrderRequest, ...]: ...


class ExecutionModel(Protocol):
    def execute(
        self,
        orders: tuple[OrderRequest, ...],
        frame: MarketFrame,
        account: AccountSnapshot,
    ) -> ExecutionResult: ...


class Account(Protocol):
    def snapshot(self) -> AccountSnapshot: ...

    def apply(self, result: ExecutionResult, frame: MarketFrame) -> AccountSnapshot: ...


class Recorder(Protocol):
    def record(self, result: StepResult) -> None: ...


@dataclass(eq=False)
class Components:
    """One run's resource bundle. No kernel, step method or execution loop.

    A factory creates a new bundle and fresh mutable plug-ins for every call.
    Configure them before yielding; ownership then belongs to the kernel until
    the resource context exits. Immutable rules may be shared.
    """

    source: DataSource
    strategy: Strategy
    risk: RiskModel
    market: MarketAdapter
    execution: ExecutionModel
    account: Account
    recorder: Recorder


class DatasetPort(Protocol):
    """The standard query boundary injected into the resident kernel."""

    def query(self, query): ...


class StrategyLoader(Protocol):
    def __call__(self, strategy_id: str, parameters: dict): ...


class ParameterUpdater(Protocol):
    def __call__(self, trials: tuple[Trial, ...]) -> Mapping[str, Scalar] | None: ...
