"""Resident query-to-settlement backtest orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from math import isfinite
from threading import Lock
from typing import Literal

from .backtest import BacktestError, BacktestRequest, BacktestResponse, SearchResponse, Trial
from .calculation import Calculation, _candidate_parameters
from .contracts import AccountSnapshot, ExecutionResult, MarketFrame, OrderRequest, StepResult, TargetSet
from .interfaces import Components


class ContractViolation(ValueError):
    pass


def _config_value(value):
    if is_dataclass(value):
        return {key: _config_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _config_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_config_value(item) for item in value]
    return value


@dataclass
class Run:
    """One offline run owns all mutable plug-ins and its audit trail."""

    components: Components
    _pending_orders: tuple[OrderRequest, ...] = field(default=(), init=False, repr=False)
    _state: Literal["ready", "active", "failed", "closed"] = field(default="ready", init=False, repr=False)
    _last_at: datetime | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def claim(self) -> None:
        with self._lock:
            if self._state != "ready":
                raise BacktestError("run_reused", "each request requires a fresh run")
            self._state = "active"

    def close(self) -> None:
        self._state = "closed"

    @property
    def pending_orders(self):
        return self._pending_orders

    def step(self, frame: MarketFrame) -> StepResult:
        if self._state == "closed":
            raise BacktestError("run_closed", "a closed run cannot continue")
        if self._state == "failed":
            raise BacktestError("run_failed", "a failed run cannot continue")
        self._state = "active"
        try:
            result = self._step(frame)
        except BaseException:
            self._state = "failed"
            raise
        self._last_at = frame.at
        return result

    def _step(self, frame):
        if not isinstance(frame, MarketFrame):
            raise TypeError("data source must yield MarketFrame")
        if self._last_at is not None and frame.at <= self._last_at:
            raise ContractViolation("market timestamps must be strictly increasing")
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
            if any(not risked.reasons.get(symbol)
                   or risked.reasons.get(symbol) == decision.reasons.get(symbol)
                   for symbol in changed):
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
    def _target(target, at, source_id, plugin):
        if not isinstance(target, TargetSet):
            raise TypeError(f"{plugin} must return TargetSet")
        if target.as_of != at:
            raise ContractViolation(f"{plugin} target timestamp changed")
        if target.source_id != source_id:
            raise ContractViolation(f"{plugin} target source changed")


class Kernel:
    """One resident coordinator with fresh strategy and settlement per request."""

    def __init__(self, data_port, strategy_loader, query_builder, settlement_type, *, calculation=Calculation):
        self._data = data_port
        self._load_strategy = strategy_loader
        self._build_query = query_builder
        self._settlement_type = settlement_type
        self._calculation = calculation
        self._lock = Lock()

    def run(self, request: BacktestRequest) -> BacktestResponse:
        self._check_request(request)
        self._claim()
        try:
            return self._run(request)
        finally:
            self._lock.release()

    def search(
        self,
        request: BacktestRequest,
        updater,
        *,
        max_trials: int,
        metric: str,
        direction: str,
        validation: BacktestRequest | None = None,
    ) -> SearchResponse:
        self._check_request(request)
        if not callable(updater):
            raise BacktestError("invalid_search", "updater must be callable")
        if type(max_trials) is not int or max_trials < 1:
            raise BacktestError("invalid_search", "max_trials must be a positive integer")
        if not isinstance(metric, str) or not metric.strip():
            raise BacktestError("invalid_search", "metric must be a non-empty string")
        if not isinstance(direction, str) or direction not in {"max", "min"}:
            raise BacktestError("invalid_search", "direction must be max or min")
        if validation is not None:
            self._check_request(validation)
            self._check_validation(request, validation)
        self._claim()
        completed: list[Trial] = []
        try:
            best_index = None
            for index in range(max_trials):
                trial_request = request
                response = None
                try:
                    values = updater(tuple(completed))
                except Exception as error:
                    raise self._search_failure(
                        trial_request, completed, str(error),
                        failed_values=getattr(error, "failed_values", None),
                        trial_index=getattr(error, "trial_index", index + 1),
                    ) from error
                if values is None:
                    if not completed:
                        raise self._search_failure(request, completed, "updater returned no first trial")
                    break
                try:
                    parameters = self._parameters(values)
                    trial_request = replace(
                        request,
                        run_id=f"{request.run_id}-trial-{index + 1:04d}",
                        parameters={**dict(request.parameters), **parameters},
                    )
                    response = self._run(trial_request)
                    score = response.metrics.get(metric)
                    if type(score) not in (int, float) or not isfinite(score):
                        raise ValueError(f"metric {metric} must be a finite number")
                    trial = Trial(trial_request, response, float(score))
                except Exception as error:
                    partial = getattr(error, "partial_result", None)
                    if partial is None and response is not None:
                        partial = response.account_result
                    raise self._search_failure(
                        trial_request, completed, str(error), partial,
                        failed_values=values, trial_index=index + 1,
                    ) from error
                completed.append(trial)
                if best_index is None:
                    best_index = index
                else:
                    current = completed[best_index].score
                    if ((direction == "max" and trial.score > current)
                            or (direction == "min" and trial.score < current)):
                        best_index = index
            if best_index is None:
                raise self._search_failure(request, completed, "search produced no trials")
            validation_response = None
            if validation is not None:
                best = completed[best_index]
                validation_request = self._validation_request(
                    request, validation, best.request.parameters,
                )
                try:
                    validation_response = self._run(validation_request)
                except Exception as error:
                    partial = getattr(error, "partial_result", None)
                    raise self._search_failure(
                        validation_request, completed, str(error), partial,
                    ) from error
            return SearchResponse(tuple(completed), best_index, validation_response)
        finally:
            self._lock.release()

    def _run(self, request: BacktestRequest) -> BacktestResponse:
        settlement = None
        try:
            strategy = self._load_strategy(request.strategy_id, dict(request.parameters))
            requirements = strategy.requirements()
            query = self._build_query(request, requirements)
            dataset = self._data.query(query)
            if dataset.dataset != query.dataset:
                raise ValueError("dataset identity changed across the query boundary")
            if query.symbols is not None and tuple(dataset.scope.get("symbols", ())) != query.symbols:
                raise ValueError("dataset symbols do not satisfy the strategy query")
            if not dataset.records[-1].trade_date <= query.end < dataset.calendar[-1]:
                raise ValueError("dataset does not cover query end before following-session sentinel")
            if (requirements.universe != "dataset" and request.universe != "dataset"
                    and request.universe != requirements.universe):
                raise ValueError("custom universe cannot lower or replace strategy universe")
            describe = getattr(self._settlement_type, "describe", None)
            if callable(describe):
                settlement_config = describe(dict(request.settlement_parameters))
            else:
                settlement_config = {
                    "module": f"{self._settlement_type.__module__}:{self._settlement_type.__name__}",
                    "params": _config_value(dict(request.settlement_parameters)),
                }
            dataset = replace(dataset, provenance={
                **dict(dataset.provenance), "settlement": settlement_config,
            })
            policies = dataset.policies
            settlement = self._settlement_type(
                request.initial_cash, policies["fees"], policies["tax_policy"],
                **dict(request.settlement_parameters),
            )
            records = dataset.records
            calendar = dataset.calendar
            if len(calendar) != len(records) + 1:
                raise ValueError("dataset must contain one following-session sentinel")
            if not records:
                raise ValueError("dataset has no historical records")
            selected = []
            for index, record in enumerate(records):
                day = calendar[index]
                following = calendar[index + 1]
                if record.trade_date != day:
                    raise ValueError("dataset record does not match its calendar day")
                frame, snapshot = settlement.apply(record, following, request.start.date())
                target = strategy.generate(frame, snapshot)
                settlement.plan(target, strategy.strategy_id)
                if day >= request.start.date():
                    selected.append((day, frame))
            result = settlement.result()
            points = [point for point in result.equity if point.day >= request.start.date()]
            if not points:
                raise ValueError("dataset produced no points in requested interval")
            final = points[-1]
            metrics = self._calculation.evaluate(
                [(point.day, float(point.equity)) for point in points],
                **dict(request.evaluation_parameters),
            )
            holdings = result.holdings[-1][1] if result.holdings else ()
            positions = {}
            for lot in holdings:
                positions[lot.symbol] = positions.get(lot.symbol, 0) + lot.shares
            fills = sum(1 for fill in result.fills if fill.trade_date >= request.start.date())
            return BacktestResponse(
                request.run_id, request.strategy_id, request.dataset_id,
                selected[0][1].at, selected[-1][1].at, len(selected), fills,
                final.cash, final.equity, positions, len(result.pending),
                account_result=result, dataset=dataset, metrics=metrics,
            )
        except Exception as error:
            if settlement is not None:
                settlement.fail(error)
                partial = settlement.result()
                if isinstance(error, BacktestError):
                    error.partial_result = partial
                    raise
                failure = BacktestError("run_failed", str(error))
                failure.partial_result = partial
                raise failure from error
            raise BacktestError("run_failed", str(error)) from error
    @staticmethod
    def _check_request(request: BacktestRequest) -> None:
        if not isinstance(request, BacktestRequest):
            raise BacktestError("invalid_request", "request must be BacktestRequest")

    def _claim(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise BacktestError("busy", "kernel already has an active run")

    @staticmethod
    def _parameters(values) -> dict:
        if not isinstance(values, Mapping):
            raise TypeError("updater must return a mapping or None")
        return _candidate_parameters(values)

    @staticmethod
    def _check_validation(request: BacktestRequest, validation: BacktestRequest) -> None:
        if validation.start.date() <= request.end.date():
            raise BacktestError("invalid_search", "validation must start after search end")
        for name in (
            "strategy_id", "dataset_id", "initial_cash", "fields", "history", "price_basis",
            "as_of", "symbols", "universe", "settlement_parameters", "evaluation_parameters",
        ):
            if getattr(validation, name) != getattr(request, name):
                raise BacktestError("invalid_search", f"validation changes {name}")

    @staticmethod
    def _validation_request(
        request: BacktestRequest,
        validation: BacktestRequest,
        parameters: Mapping,
    ) -> BacktestRequest:
        return replace(
            validation,
            run_id=f"{request.run_id}-validation",
            strategy_id=request.strategy_id,
            dataset_id=request.dataset_id,
            parameters=dict(parameters),
        )

    @staticmethod
    def _search_failure(
        request, completed, detail, partial=None, *,
        failed_values=None, trial_index=None,
    ):
        error = BacktestError("search_failed", detail)
        error.completed_trials = tuple(completed)
        error.failed_request = request
        if partial is not None:
            error.partial_result = partial
        if failed_values is not None:
            error.failed_values = failed_values
        if trial_index is not None:
            error.trial_index = trial_index
        return error
