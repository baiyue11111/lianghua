"""Resident query-to-settlement backtest orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite
from threading import Lock

from protocal import (
    BacktestError,
    BacktestRequest,
    BacktestResponse,
    SearchResponse,
    Trial,
    _config_value,
    _strategy_params,
    build_effective_config,
    build_execution_hash,
)
from .calculation import Calculation, _candidate_parameters


@dataclass(frozen=True)
class Preparation:
    request: BacktestRequest
    strategy: object
    requirements: object
    query: object
    dataset: object
    effective_config: dict
    config_hash: str


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

    def prepare(self, request: BacktestRequest) -> Preparation:
        self._check_request(request)
        strategy = self._load_strategy(request.strategy_id, dict(request.parameters))
        requirements = strategy.requirements()
        query = self._build_query(request, requirements)
        describe = getattr(self._settlement_type, "describe", None)
        if callable(describe):
            settlement_config = describe(dict(request.settlement_parameters))
        else:
            settlement_config = {
                "module": f"{self._settlement_type.__module__}:{self._settlement_type.__name__}",
                "params": _config_value(dict(request.settlement_parameters)),
            }
        try:
            dataset = self._data.query(query)
        except Exception as error:
            error.effective_params = _strategy_params(strategy, request)
            error.requirements = _config_value(requirements)
            error.effective_config = build_effective_config(
                request, strategy, requirements, None, settlement_config,
            )[0]
            raise
        if dataset.dataset != query.dataset:
            raise ValueError("dataset identity changed across the query boundary")
        if query.symbols is not None and tuple(dataset.scope.get("symbols", ())) != query.symbols:
            raise ValueError("dataset symbols do not satisfy the strategy query")
        validate_dataset = getattr(self._settlement_type, "validate_dataset", None)
        if callable(validate_dataset):
            validate_dataset(dataset, request, **dict(request.settlement_parameters))
        if not dataset.records[-1].trade_date <= query.end < dataset.calendar[-1]:
            raise ValueError("dataset does not cover query end before following-session sentinel")
        market = dataset.scope.get("market")
        if (not isinstance(market, Mapping)
                or not isinstance(market.get("selection"), Mapping)
                or market["selection"].get("scope") != request.market["scope"]):
            raise ValueError("market resolution is missing")
        effective_config, config_hash = build_effective_config(
            request, strategy, requirements, dataset, settlement_config,
        )
        dataset = replace(dataset, provenance={
            **dict(dataset.provenance),
            "execution_hash": build_execution_hash(dataset.provenance, settlement_config),
            "settlement": settlement_config,
            "effective_config": effective_config,
            "config_hash": config_hash,
        })
        return Preparation(request, strategy, requirements, query, dataset,
                           effective_config, config_hash)

    def search(
        self,
        request: BacktestRequest,
        updater,
        *,
        max_trials: int,
        metric: str,
        direction: str,
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
            return SearchResponse(tuple(completed), best_index)
        finally:
            self._lock.release()

    def _run(self, request: BacktestRequest) -> BacktestResponse:
        settlement = None
        try:
            prepared = self.prepare(request)
            strategy = prepared.strategy
            requirements = prepared.requirements
            dataset = prepared.dataset
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
