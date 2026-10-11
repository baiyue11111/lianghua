"""Resident backtest use case and its isolated, single-use run state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Literal
from weakref import WeakSet

from .backtest import BacktestError, BacktestRequest, BacktestResponse

from .contracts import (
    AccountSnapshot,
    ExecutionResult,
    MarketFrame,
    OrderRequest,
    StepResult,
    TargetSet,
)
from .interfaces import Components, RunFactory


class ContractViolation(ValueError):
    """A plugin returned a value that cannot belong to the current step."""


@dataclass
class Run:
    """One run owns all mutable plug-ins. Never share them between runs.

    The factory supplies the source and fresh plug-ins. The fixed kernel owns
    execution order. A failed run cannot be resumed; create a fresh run instead.
    """

    components: Components
    _pending_orders: tuple[OrderRequest, ...] = field(default=(), init=False, repr=False)
    _state: Literal['ready', 'active', 'failed', 'closed'] = field(default='ready', init=False, repr=False)
    _last_at: datetime | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def claim(self) -> None:
        with self._lock:
            if self._state != 'ready':
                raise BacktestError('run_reused', 'each request requires a fresh run')
            self._state = 'active'

    def close(self) -> None:
        """Seal the run before its factory releases external resources."""
        self._state = 'closed'

    @property
    def pending_orders(self) -> tuple[OrderRequest, ...]:
        return self._pending_orders

    def step(self, frame: MarketFrame) -> StepResult:
        if self._state == 'closed':
            raise BacktestError('run_closed', 'a closed run cannot continue')
        if self._state == 'failed':
            raise BacktestError('run_failed', 'a failed run cannot continue')
        self._state = 'active'
        try:
            result = self._step(frame)
        except BaseException:
            self._state = 'failed'
            raise
        self._last_at = frame.at
        return result

    def _step(self, frame: MarketFrame) -> StepResult:
        if not isinstance(frame, MarketFrame):
            raise TypeError("data source must yield MarketFrame")
        if self._last_at is not None and frame.at <= self._last_at:
            raise ContractViolation('market timestamps must be strictly increasing')
        before = self.components.account.snapshot()
        if not isinstance(before, AccountSnapshot):
            raise TypeError("account snapshot must be AccountSnapshot")

        execution = self.components.execution.execute(self._pending_orders, frame, before)
        if not isinstance(execution, ExecutionResult):
            raise TypeError("execution model must return ExecutionResult")
        if execution.as_of != frame.at:
            raise ContractViolation("execution timestamp must equal market timestamp")
        if execution.orders != self._pending_orders:
            raise ContractViolation("execution model changed pending orders")

        after = self.components.account.apply(execution, frame)
        if not isinstance(after, AccountSnapshot):
            raise TypeError("account must return AccountSnapshot")
        if after.as_of != frame.at:
            raise ContractViolation("account timestamp must equal market timestamp")

        decision = self.components.strategy.generate(frame, after)
        if decision is None:
            risked = None
            orders = execution.remaining
        else:
            self._target(decision, frame.at, self.components.strategy.strategy_id, "strategy")
            risked = self.components.risk.apply(decision, after)
            self._target(risked, frame.at, self.components.strategy.strategy_id, "risk model")
            changed = {
                symbol for symbol in set(risked.weights) | set(decision.weights)
                if risked.weights.get(symbol) != decision.weights.get(symbol)
            }
            if any(not risked.reasons.get(symbol) or
                   risked.reasons.get(symbol) == decision.reasons.get(symbol) for symbol in changed):
                raise ContractViolation("risk changes require an explicit reason for each changed target")
            orders = self.components.market.plan(risked, frame, after, execution.remaining)
        if not isinstance(orders, tuple) or any(not isinstance(order, OrderRequest) for order in orders):
            raise TypeError("market adapter must return tuple[OrderRequest, ...]")
        retained = set(execution.remaining)
        if any(order.created_at != frame.at and order not in retained for order in orders):
            raise ContractViolation("market adapter introduced a stale order")
        self._pending_orders = orders
        result = StepResult(frame, execution, after, decision, risked, orders)
        self.components.recorder.record(result)
        return result

    def run(self) -> tuple[StepResult, ...]:
        """Replay the source and collect its detailed audit trail."""
        self.claim()
        try:
            return tuple(self.step(frame) for frame in self.components.source)
        finally:
            self.close()

    @staticmethod
    def _target(target: TargetSet, at: datetime, source_id: str, plugin: str) -> None:
        if not isinstance(target, TargetSet):
            raise TypeError(f"{plugin} must return TargetSet")
        if target.as_of != at:
            raise ContractViolation(f"{plugin} target timestamp changed")
        if target.source_id != source_id:
            raise ContractViolation(f"{plugin} target source changed")


class Kernel:
    """Fixed orchestration, resident across independent backtest requests.

    One instance admits one request at a time; concurrent requests fail explicitly.
    Only the factory persists. Account, strategy and orders live in the new Run.
    """

    def __init__(self, factory: RunFactory) -> None:
        self._factory = factory
        self._lock = Lock()
        self._used: WeakSet[Components] = WeakSet()

    def run(self, request: BacktestRequest) -> BacktestResponse:
        if not isinstance(request, BacktestRequest):
            raise BacktestError('invalid_request', 'request must be BacktestRequest')
        if not self._lock.acquire(blocking=False):
            raise BacktestError('busy', 'kernel already has an active run')
        try:
            response = None
            with self._factory.open(request) as components:
                if not isinstance(components, Components):
                    raise TypeError('factory must provide Components')
                if components in self._used:
                    raise BacktestError('run_reused', 'each request requires fresh components')
                self._used.add(components)
                run = Run(components)
                run.claim()
                try:
                    response = self._execute(run, request)
                finally:
                    run.close()
            if response is None:
                raise BacktestError('run_failed', 'factory suppressed a run failure')
            return response
        except BacktestError:
            raise
        except Exception as error:
            raise BacktestError('run_failed', str(error)) from error
        finally:
            self._lock.release()

    @staticmethod
    def _execute(run: Run, request: BacktestRequest) -> BacktestResponse:
        if run.components.strategy.strategy_id != request.strategy_id:
            raise BacktestError('strategy_mismatch', 'factory selected a different strategy')
        initial = run.components.account.snapshot()
        if not isinstance(initial, AccountSnapshot):
            raise TypeError('account snapshot must be AccountSnapshot')
        if (initial.cash != request.initial_cash or initial.equity != request.initial_cash
                or initial.positions or initial.sellable or initial.as_of >= request.start):
            raise BacktestError('account_mismatch', 'factory must create the requested fresh cash account')
        steps = fills = 0
        first_at = None
        last = None
        for frame in run.components.source:
            if not isinstance(frame, MarketFrame):
                raise TypeError('data source must yield MarketFrame')
            if not request.start <= frame.at <= request.end:
                raise BacktestError('frame_out_of_range', 'market frame lies outside the requested interval')
            last = run.step(frame)
            if first_at is None:
                first_at = frame.at
            steps += 1
            fills += len(last.execution.fills)
        if last is None:
            raise BacktestError('empty_source', 'no market frames in the requested interval')
        return BacktestResponse(
            request.run_id, request.strategy_id, request.dataset_id,
            first_at, last.frame.at, steps, fills,
            last.account.cash, last.account.equity, last.account.positions,
            len(last.planned_orders),
        )
