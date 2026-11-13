"""Resident query-to-settlement backtest orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from math import isfinite
from threading import Lock
from zoneinfo import ZoneInfo

from protocol import AccountSnapshot, BacktestError, BacktestRequest, BacktestResponse, SearchResponse, Side, TargetSet, Trial, _config_value, _strategy_params, build_effective_config, build_execution_hash
from .calculation import AllocationPlan, Calculation, TradeRules, _candidate_parameters


@dataclass(frozen=True)
class Preparation:
    request: BacktestRequest
    strategy: object
    requirements: object
    query: object
    dataset: object
    effective_config: dict
    config_hash: str


@dataclass(frozen=True)
class Prediction:
    signal_date: date
    next_session: date
    target: TargetSet | None
    allocation: AllocationPlan
    account: AccountSnapshot
    prices: Mapping
    buy_value: Decimal
    cash_gap: Decimal
    provenance: Mapping
    effective_config: dict
    config_hash: str
    status: str = 'reference_orders'


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

    def predict(self, request: BacktestRequest, accounts, *, pending) -> Prediction:
        """Replay actual daily account snapshots and size the final close's target.

        This produces estimates, not fills or submitted orders. All snapshots,
        including warm-up days, must be supplied by the caller.
        """
        if not isinstance(pending, (list, tuple)) or pending:
            raise ValueError('an explicit empty pending order list is required')
        if not isinstance(accounts, Mapping):
            raise ValueError('daily account snapshots are required')
        if request.settlement_parameters.get('accounting', 'account') != 'account':
            raise ValueError('prediction requires account accounting and real shares')
        self._claim()
        try:
            prepared = self.prepare(request)
            dataset = prepared.dataset
            records = dataset.records
            calendar = dataset.calendar
            if (not records or len(calendar) != len(records) + 1
                    or records[-1].trade_date != request.end.date()):
                raise ValueError('data must end on the requested decision day and include the next session')
            now = datetime.now(timezone.utc)
            if records[-1].frame.at > now:
                raise ValueError('decision close is in the future')
            days = tuple(record.trade_date for record in records)
            if tuple(calendar[:-1]) != days or any(a >= b for a, b in zip(calendar, calendar[1:])):
                raise ValueError('records and calendar must be chronological and aligned')
            if any(type(day) is not date for day in accounts):
                raise ValueError('account snapshot keys must be dates')
            missing = sorted(set(days) - set(accounts))
            extra = sorted(set(accounts) - set(days))
            if missing or extra:
                raise ValueError('account snapshot dates differ: '
                    f"missing={[day.isoformat() for day in missing]}, "
                    f"extra={[day.isoformat() for day in extra]}")
            params = dict(request.settlement_parameters)
            limits, execution = self._settlement_type.resolve(**{
                key: params[key] for key in ('limits', 'execution', 'accounting') if key in params
            })
            if execution['check']['model'] != 'cn_a_share':
                raise ValueError('prediction requires a known market opening time')
            opening = datetime.combine(calendar[-1], time(9, 25), tzinfo=ZoneInfo('Asia/Shanghai'))
            if now >= opening:
                raise ValueError('the next-session open has passed')
            rules = TradeRules(execution, dataset.policies['fees'], dataset.policies['tax_policy'])
            target = None
            for index, record in enumerate(records):
                account = accounts[record.trade_date]
                self._check_prediction_account(account, record)
                following = calendar[index + 1]
                frame = replace(record.frame, context={**record.frame.context,
                    'next_session': following.isoformat(),
                    'trading': record.trade_date >= request.start.date()})
                target = prepared.strategy.generate(frame, account)
                if target is not None and (not isinstance(target, TargetSet)
                        or target.as_of != frame.at or target.source_id != prepared.strategy.strategy_id):
                    raise ValueError('strategy target identity/time do not match decision')
            record = records[-1]
            account = accounts[record.trade_date]
            if target is None:
                allocation = AllocationPlan((), ())
            else:
                allocation = rules.allocate(target, next_session=calendar[-1],
                    equity=account.equity,
                    holdings={symbol: int(shares) for symbol, shares in account.positions.items()},
                    raw_closes=record.closing_marks, amount20=record.closing_amount20,
                    limits=limits)
                if any(order.side == Side.SELL and order.requested_shares > account.sellable[order.symbol]
                       for order in allocation.orders):
                    raise ValueError('sell order exceeds the account sellable shares')
            symbols = set(account.positions) | (set(target.weights) if target is not None else set())
            prices = {symbol: record.closing_marks[symbol] for symbol in sorted(symbols)}
            buy_value = sum((prices[order.symbol] * order.requested_shares
                             for order in allocation.orders if order.side == Side.BUY), Decimal(0))
            cash_gap = max(Decimal(0), buy_value - account.cash)
            if datetime.now(timezone.utc) >= opening:
                raise ValueError('the next-session opening auction has passed')
            return Prediction(record.trade_date, calendar[-1], target, allocation, account,
                              prices, buy_value, cash_gap, dataset.provenance,
                              prepared.effective_config, prepared.config_hash)
        finally:
            self._lock.release()

    @staticmethod
    def _check_prediction_account(account, record):
        if not isinstance(account, AccountSnapshot) or account.as_of != record.frame.at:
            raise ValueError('account snapshot must match the market close timestamp')
        if account.cash < 0 or account.equity <= 0:
            raise ValueError('account cash must be nonnegative and equity positive')
        if set(account.positions) != set(account.sellable):
            raise ValueError('sellable shares are required for each holding')
        for symbol, shares in account.positions.items():
            sellable = account.sellable[symbol]
            if (shares <= 0 or shares != shares.to_integral_value()
                    or sellable < 0 or sellable != sellable.to_integral_value() or sellable > shares):
                raise ValueError('invalid account position or sellable shares')
            if symbol not in record.closing_marks or record.closing_marks[symbol] <= 0:
                raise ValueError(f'missing close price for holding: {symbol}')

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
            # Terminal delisting evidence is data, not configuration: the settlement
            # must never infer it from a missing bar (suspension and delisting differ).
            settlement_parameters = dict(request.settlement_parameters)
            dataset_inputs = getattr(self._settlement_type, "dataset_parameters", None)
            if callable(dataset_inputs):
                settlement_parameters.update(dataset_inputs(dataset))
            settlement = self._settlement_type(
                request.initial_cash, policies["fees"], policies["tax_policy"],
                **settlement_parameters,
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
