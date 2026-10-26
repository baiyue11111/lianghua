"""Standard contracts and pure input validation."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Protocol as TypingProtocol, TypeAlias
from uuid import uuid4

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


JsonObject: TypeAlias = dict[str, JsonValue]


PortKind: TypeAlias = Literal["backtests", "research", "sources", "data", "commands"]


PROTOCOL_NAME = "lianghua.ui"


PROTOCOL_VERSION = 1


PROBLEM_SCHEMA = "urn:lianghua:ui:problem:v1"


EXECUTION_FIELDS = ("open", "high", "low", "close", "volume", "amount")


LIQUIDITY_WINDOW = 20


def required_fields(fields) -> tuple[str, ...]:
    """Keep settlement prices and strategy fields in one ordered request."""
    return tuple(dict.fromkeys((*EXECUTION_FIELDS, *tuple(fields))))


@dataclass(frozen=True)
class DataRef:
    kind: str
    identity: str
    name: str
    file: str
    sha256: str
    size: int


class DataError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


Scalar: TypeAlias = str | int | float | bool | None


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _name(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite Decimal{' greater than zero' if positive else ''}")


def _floats(values: Mapping[str, float], name: str) -> Mapping[str, float]:
    copied = dict(values)
    for key, value in copied.items():
        _name(key, f"{name} key")
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(f"{name} values must be finite numbers")
    return MappingProxyType(copied)


def _decimals(values: Mapping[str, Decimal], name: str) -> Mapping[str, Decimal]:
    copied = dict(values)
    for key, value in copied.items():
        _name(key, f"{name} key")
        _decimal(value, f"{name} value")
    return MappingProxyType(copied)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Bar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    @classmethod
    def open_bar(cls, symbol: str, trade_date: date, price: float, volume: float, amount: float) -> "Bar":
        return cls(symbol, trade_date, price, price, price, price, volume, amount)


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: Side
    requested_shares: int
    signal_date: date
    submit_date: date
    reason: str


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    trade_date: date
    shares: int
    price: float
    gross_amount: float
    commission: float
    stamp_duty: float
    status: str
    reason: str | None = None
    transfer_fee: float = 0.0


@dataclass(frozen=True)
class DataRequest:
    fields: tuple[str, ...]
    history_bars: int
    frequency: str = "daily"
    price_basis: str = "adjusted"

    def __post_init__(self) -> None:
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("unique required data fields are required")
        for name in self.fields:
            _name(name, "data field")
        if type(self.history_bars) is not int or self.history_bars < 1:
            raise ValueError("history_bars must be a positive integer")
        _name(self.frequency, "data frequency")
        _name(self.price_basis, "price basis")


@dataclass(frozen=True)
class HistoryBar:
    at: datetime
    fields: Mapping[str, Scalar]

    def __post_init__(self) -> None:
        _aware(self.at, "history timestamp")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class MarketFrame:
    at: datetime
    venue: str
    fields: Mapping[str, Mapping[str, Scalar]]
    context: Mapping[str, Scalar] = field(default_factory=dict)
    history: Mapping[str, tuple[HistoryBar, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.at, "market timestamp")
        _name(self.venue, "venue")
        copied = {}
        for instrument, values in self.fields.items():
            _name(instrument, "instrument")
            if not isinstance(values, Mapping):
                raise TypeError("instrument fields must be a mapping")
            copied[instrument] = MappingProxyType(dict(values))
        object.__setattr__(self, "fields", MappingProxyType(copied))
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))
        history = {}
        for instrument, rows in self.history.items():
            _name(instrument, "history instrument")
            rows = tuple(rows)
            previous = None
            for row in rows:
                if not isinstance(row, HistoryBar):
                    raise TypeError("history rows must be HistoryBar")
                if row.at > self.at or previous is not None and row.at <= previous:
                    raise ValueError("history must be ordered, unique and not future")
                previous = row.at
            history[instrument] = rows
        object.__setattr__(self, "history", MappingProxyType(history))


@dataclass(frozen=True)
class TargetSet:
    source_id: str
    as_of: datetime
    weights: Mapping[str, float]
    reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.source_id, "source_id")
        _aware(self.as_of, "target timestamp")
        object.__setattr__(self, "weights", _floats(self.weights, "weights"))
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))


@dataclass(frozen=True)
class AccountSnapshot:
    as_of: datetime
    cash: Decimal
    equity: Decimal
    positions: Mapping[str, Decimal]
    sellable: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.as_of, "account timestamp")
        _decimal(self.cash, "cash")
        _decimal(self.equity, "equity")
        object.__setattr__(self, "positions", _decimals(self.positions, "positions"))
        object.__setattr__(self, "sellable", _decimals(self.sellable, "sellable"))


@dataclass(frozen=True)
class RegistrationAction:
    distribution_id: str
    event: object
    source_sha256: str
    share_credit_date: date | None
    stock_nominal: Decimal | None
    reserve_taxable: Decimal | None
    tax_source_sha256: str | None


@dataclass(frozen=True)
class HistoryDay:
    trade_date: date
    frame: MarketFrame
    bars: Mapping[str, Bar]
    previous_closes: Mapping[str, float | None]
    opening_marks: Mapping[str, Decimal]
    closing_marks: Mapping[str, Decimal]
    opening_amount20: Mapping[str, Decimal]
    closing_amount20: Mapping[str, Decimal]
    liquidity_as_of: date
    registrations: tuple[RegistrationAction, ...]
    opening_volume20: Mapping[str, Decimal] = field(default_factory=dict)
    opening_eligible: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RegistrationRecord:
    action: RegistrationAction
    entitlement: object
    deliveries: tuple[object, ...]


@dataclass(frozen=True)
class HistoryResult:
    frames: tuple[MarketFrame, ...]
    targets: tuple[TargetSet | None, ...]
    allocations: tuple[object, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    pending: tuple[Order, ...]
    cancellations: tuple[object, ...]
    cash_events: tuple[object, ...]
    equity: tuple[object, ...]
    holdings: tuple[object, ...]
    distribution_postings: tuple[object, ...]
    registrations: tuple[object, ...]
    tax_payments: tuple[object, ...]
    tax_liabilities: tuple[object, ...]
    tax_pending: tuple[object, ...]
    tax_holdings: tuple[object, ...]


@dataclass(frozen=True)
class DataQuery:
    """One explicit request for a registered dataset slice."""

    dataset: str
    start: date
    end: date
    fields: tuple[str, ...]
    history: int
    price_basis: str
    as_of: date | None = None
    symbols: tuple[str, ...] | None = None
    universe: str = "dataset"
    frequency: str = "daily"
    purpose: str = "trade"
    market: Mapping[str, object] | None = None
    tax: str = "fifo"
    accounting: str = "account"

    def __post_init__(self):
        if self.accounting not in {"account", "research"}:
            raise ValueError("unsupported accounting model")
        if self.accounting == 'research' and self.tax != 'none':
            raise ValueError('research accounting requires tax model none')
        if self.tax not in {"fifo", "none"}:
            raise ValueError("unsupported query tax model")
        if not isinstance(self.dataset, str) or not self.dataset.strip():
            raise ValueError("dataset is required")
        if not isinstance(self.start, date) or not isinstance(self.end, date) or self.start > self.end:
            raise ValueError("ordered query dates are required")
        if self.as_of is not None and (not isinstance(self.as_of, date) or self.as_of < self.end):
            raise ValueError("as_of must cover the query end date")
        if not self.fields or any(not isinstance(field, str) or not field.strip() for field in self.fields):
            raise ValueError("explicit query fields are required")
        object.__setattr__(self, "fields", tuple(self.fields))
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("query fields must be unique")
        if type(self.history) is not int or self.history < 1:
            raise ValueError("history must be a positive integer")
        if self.price_basis not in {"adjusted", "raw"}:
            raise ValueError("unsupported query price basis")
        if self.purpose not in {"trade", "compare"}:
            raise ValueError("unsupported query purpose")
        if not isinstance(self.universe, str) or not self.universe.strip():
            raise ValueError("universe is required")
        if self.symbols is not None:
            symbols = tuple(self.symbols)
            object.__setattr__(self, "symbols", symbols)
        if self.symbols is not None and (not self.symbols or
                any(not isinstance(symbol, str) or not symbol.strip() for symbol in self.symbols) or
                len(set(self.symbols)) != len(self.symbols)):
            raise ValueError("symbols must be ordered and unique")
        if self.market is not None:
            object.__setattr__(self, "market", _market_selection(self.market))


@dataclass(frozen=True)
class Dataset:
    """Prepared data plus the evidence needed by the account settlement."""

    dataset: str
    records: tuple["HistoryDay", ...]
    policies: Mapping[str, object]
    provenance: Mapping[str, object]
    scope: Mapping[str, object]
    calendar: tuple[date, ...]
    fields: tuple[str, ...]
    history: int

    def __post_init__(self):
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "calendar", tuple(self.calendar))
        object.__setattr__(self, "fields", tuple(self.fields))
        if (not self.fields or any(not isinstance(field, str) or not field.strip()
                                   for field in self.fields)
                or len(set(self.fields)) != len(self.fields)):
            raise ValueError("dataset fields must be non-empty and unique")
        if type(self.history) is not int or self.history < 1:
            raise ValueError("dataset history must be positive")
        object.__setattr__(self, "policies", MappingProxyType(dict(self.policies)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        if not self.dataset or not self.records:
            raise ValueError("dataset records must be a tuple")
        if len(self.calendar) != len(self.records) + 1:
            raise ValueError("dataset calendar must include one following-session sentinel")
        if any(not isinstance(day, date) for day in self.calendar):
            raise TypeError("dataset calendar must contain dates")
        if any(not hasattr(record, "trade_date") or not hasattr(record, "frame")
               or record.trade_date != self.calendar[index]
               or record.frame.at.date() != record.trade_date
               for index, record in enumerate(self.records)):
            raise ValueError("dataset record dates must match the calendar")
        if tuple(sorted(set(self.calendar))) != self.calendar:
            raise ValueError("dataset calendar must be ordered and unique")
        if not isinstance(self.policies, Mapping) or not isinstance(self.provenance, Mapping) or not self.scope:
            raise TypeError("dataset policies and provenance must be mappings")


class DataPort(TypingProtocol):
    """The single public data bus contract used by module consumers."""

    def query(self, query: DataQuery) -> Dataset: ...
    def read_ref(self, ref: DataRef) -> bytes: ...
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None, folder: str = "") -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
    def delete(self, kind: str, identity: str, name: str) -> None: ...
    def manifest(self, dataset: str) -> Mapping[str, object]: ...
    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef: ...
    def transaction(self, namespace: str): ...
    def source_facts(self) -> Mapping[str, object]: ...
    def profile_report(self) -> Mapping[str, object]: ...
    def research_report(self) -> Mapping[str, object]: ...
    def doctor_report(self) -> Mapping[str, object]: ...


class BacktestError(ValueError):
    """Stable machine-readable error for the protocol and kernel boundary."""

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
    market: Mapping[str, object] = field(default_factory=lambda: {"scope": "CN_A"})
    settlement_parameters: Mapping[str, object] | None = None
    evaluation_parameters: Mapping[str, object] | None = None
    benchmark_id: str | None = None

    def __post_init__(self) -> None:
        try:
            for name in ("run_id", "strategy_id", "dataset_id"):
                _name(getattr(self, name), name)
            _aware(self.start, "start")
            _aware(self.end, "end")
            if self.start > self.end:
                raise ValueError("start must not exceed end")
            _decimal(self.initial_cash, "initial_cash", positive=True)
            fields = tuple(self.fields)
            if fields and (len(set(fields)) != len(fields)
                           or any(not isinstance(item, str) or not item.strip() for item in fields)):
                raise ValueError("fields must be unique non-empty strings")
            object.__setattr__(self, "fields", fields)
            if type(self.history) is not int or self.history < 0:
                raise ValueError("history must be a non-negative integer")
            if self.price_basis != "adjusted":
                raise ValueError("unsupported price basis")
            if self.as_of is not None and not isinstance(self.as_of, date):
                raise ValueError("as_of must be a date")
            object.__setattr__(self, "market", _market_selection(self.market))
            if self.benchmark_id is not None:
                _name(self.benchmark_id, "benchmark_id")
            for name in ("settlement_parameters", "evaluation_parameters"):
                value = getattr(self, name)
                if value is None:
                    object.__setattr__(self, name, MappingProxyType({}))
                elif not isinstance(value, Mapping):
                    raise TypeError(f"{name} must be a mapping")
                else:
                    object.__setattr__(self, name, MappingProxyType(dict(value)))
            evaluation = dict(self.evaluation_parameters)
            if set(evaluation) - {"periods", "risk_free_rate"}:
                raise ValueError("unsupported evaluation parameters")
            if "periods" in evaluation and (type(evaluation["periods"]) is not int
                                              or evaluation["periods"] < 1):
                raise ValueError("evaluation periods must be a positive integer")
            if "risk_free_rate" in evaluation:
                raw = evaluation["risk_free_rate"]
                if type(raw) not in (int, float):
                    raise ValueError("risk_free_rate must be numeric")
                try:
                    finite = isfinite(raw)
                except OverflowError as error:
                    raise ValueError("risk_free_rate must be a finite annual rate greater than -1") from error
                if not finite or raw <= -1:
                    raise ValueError("risk_free_rate must be a finite annual rate greater than -1")
                evaluation["risk_free_rate"] = float(raw)
            object.__setattr__(self, "evaluation_parameters", MappingProxyType(evaluation))
            if not isinstance(self.parameters, Mapping):
                raise TypeError("parameters must be a mapping")
            params = dict(self.parameters)
            for key, value in params.items():
                _name(key, "parameter name")
                if type(value) not in (str, int, float, bool, type(None)):
                    raise TypeError("parameter values must be scalars")
                if type(value) is float and not isfinite(value):
                    raise ValueError("parameter numbers must be finite")
            object.__setattr__(self, "parameters", MappingProxyType(params))
        except (TypeError, ValueError) as error:
            raise BacktestError("invalid_request", str(error)) from error


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
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        metrics = dict(self.metrics)
        for key, value in metrics.items():
            if not isinstance(key, str):
                raise TypeError("metric keys must be strings")
            if value is not None and (type(value) not in (int, float) or not isfinite(value)):
                raise ValueError("metric values must be finite numbers or None")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))


@dataclass(frozen=True)
class Trial:
    request: BacktestRequest
    response: BacktestResponse
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.request, BacktestRequest):
            raise TypeError("trial request must be BacktestRequest")
        if not isinstance(self.response, BacktestResponse):
            raise TypeError("trial response must be BacktestResponse")
        if type(self.score) not in (int, float) or not isfinite(self.score):
            raise ValueError("trial score must be a finite number")
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True)
class SearchResponse:
    trials: tuple[Trial, ...]
    best_index: int

    def __post_init__(self) -> None:
        trials = tuple(self.trials)
        if not trials:
            raise ValueError("search requires at least one trial")
        if type(self.best_index) is not int or not 0 <= self.best_index < len(trials):
            raise ValueError("best_index must point to a trial")
        if any(not isinstance(trial, Trial) for trial in trials):
            raise TypeError("trials must contain Trial values")
        object.__setattr__(self, "trials", trials)


def validate_facts(payload, *, kind, code, start=None, end=None, year=None):
    """Validate and return a standard fact document for every reader."""
    keys = {"schema", "kind", "code", "start", "end", "year", "source_hash", "records"}
    if not isinstance(payload, dict) or set(payload) != keys or payload["schema"] != 1:
        raise ValueError("standard fact document schema or fields are invalid")
    expected = {"kind": kind, "code": code, "start": start, "end": end, "year": year}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{kind} fact identity or range mismatch")
    source_hash = payload.get("source_hash")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError(f"{kind} fact source hash is invalid")
    if not isinstance(payload.get("records"), list):
        raise ValueError(f"{kind} fact records are malformed")
    if normalize_security_id(code) != code:
        raise ValueError("standard fact code is not canonical")
    records = payload["records"]
    if kind == "adjust":
        start_date = _fact_date(start, "adjustment start")
        end_date = _fact_date(end, "adjustment end")
        if start_date > end_date or year is not None:
            raise ValueError("adjustment fact range is invalid")
        previous = None
        for row in records:
            if not isinstance(row, dict) or set(row) != {"date", "forward", "backward"}:
                raise ValueError("adjustment fact fields are invalid")
            effective = _fact_date(row["date"], "adjustment date")
            if not start_date <= effective <= end_date or (previous is not None and effective <= previous):
                raise ValueError("adjustment fact dates are outside range or unordered")
            _fact_number(row["forward"], "forward factor", positive=True)
            _fact_number(row["backward"], "backward factor", positive=True)
            previous = effective
    elif kind == "dividend":
        if start is not None or end is not None or not isinstance(year, str) or not re.fullmatch(r"\d{4}", year):
            raise ValueError("dividend fact year is invalid")
        fields = {"announce_date", "registration_date", "operation_date", "payment_date",
                  "stock_market_date", "cash_per_share", "stock_dividend_per_share",
                  "reserve_to_stock_per_share", "cash_after_tax_text"}
        for row in records:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("dividend fact fields are invalid")
            for field in ("announce_date", "registration_date", "operation_date", "payment_date", "stock_market_date"):
                _fact_date(row[field], f"dividend {field}", required=False)
            for field in ("cash_per_share", "stock_dividend_per_share", "reserve_to_stock_per_share"):
                amount = row[field]
                if amount is None:
                    continue
                amount = _fact_number(amount, f"dividend {field}")
                if amount < 0:
                    raise ValueError(f"dividend {field} is negative")
            if row["cash_after_tax_text"] is not None and not isinstance(row["cash_after_tax_text"], str):
                raise ValueError("dividend cash_after_tax_text is invalid")
    elif kind == "security":
        if start is not None or end is not None or year is not None or len(records) != 1:
            raise ValueError("security fact identity or record count is invalid")
        row = records[0]
        if not isinstance(row, dict) or set(row) != {"listing_date", "delisting_date", "type", "status"}:
            raise ValueError("security fact fields are invalid")
        _fact_date(row["listing_date"], "listing date", required=False)
        _fact_date(row["delisting_date"], "delisting date", required=False)
        if not isinstance(row["type"], str) or not isinstance(row["status"], str):
            raise ValueError("security fact type or status is invalid")
    else:
        raise ValueError("unsupported standard fact kind")
    return records, source_hash


def _fact_date(value, field, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _fact_number(value, field, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{field} is invalid")
    return number


def _calendar_facts(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise ValueError("calendar sessions are invalid")
    raw_coverage = payload.get("coverage")
    if not isinstance(raw_coverage, list) or not raw_coverage:
        raise ValueError("calendar coverage is required")
    sessions = tuple(_fact_date(value, "calendar session") for value in payload["sessions"])
    if len(sessions) < 2 or tuple(sorted(set(sessions))) != sessions:
        raise ValueError("calendar sessions must be ordered and include a following session")
    coverage = []
    for item in raw_coverage:
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            raise ValueError("calendar coverage entry is invalid")
        start = _fact_date(item["start"], "calendar coverage start")
        end = _fact_date(item["end"], "calendar coverage end")
        if start > end:
            raise ValueError("calendar coverage range is reversed")
        coverage.append((start, end))
    coverage = tuple(sorted(coverage))
    if any(left[1] >= right[0] for left, right in zip(coverage, coverage[1:])):
        raise ValueError("calendar coverage ranges overlap")
    return sessions, coverage


def _calendar_covers(coverage, start, end):
    return any(left <= start and end <= right for left, right in coverage)


def normalize_security_id(code: str) -> str:
    """Return the stable identity used by the standard data merge key.

    A market-qualified code is authoritative.  An unqualified six-digit stock
    code keeps the existing A-share inference, while indexes and other
    instruments must carry their full market-qualified identity.
    """
    if not isinstance(code, str):
        raise ValueError("security code must be a string")
    value = code.strip().lower()
    if "." in value:
        market, number = value.split(".", 1)
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,7}", market)
                or not re.fullmatch(r"\d{6}", number)):
            raise ValueError(f"invalid market-qualified security code: {code}")
        return f"{market}.{number}"
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError(f"security code must be six digits or market-qualified: {code}")
    if value.startswith("6"):
        return f"sh.{value}"
    if value.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return f"sz.{value}"
    raise ValueError(f"unsupported market code: {code}")


@dataclass(frozen=True)
class SearchSpec:
    updater: object
    max_trials: int
    metric: str
    direction: str
    declaration: dict[str, object]


def _market_selection(value):
    def finite_scalar(item):
        if isinstance(item, (dict, list, tuple)):
            raise ValueError("market filter value must be scalar")
        if isinstance(item, Decimal) and not item.is_finite():
            raise ValueError("market filter number must be finite")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("market filter number must be finite")

    if not isinstance(value, Mapping):
        raise ValueError("market must be an object")
    unknown = set(value) - {"scope", "index", "codes", "filters"}
    if unknown:
        raise ValueError(f"unsupported market fields: {', '.join(sorted(unknown))}")
    scope = value.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("market scope must be a non-empty string")
    result = {"scope": scope.strip()}
    if "index" in value:
        index = value["index"]
        if not isinstance(index, str) or not index.strip():
            raise ValueError("market index must be a non-empty string")
        result["index"] = index.strip()
    if "codes" in value:
        codes = value["codes"]
        if (not isinstance(codes, (list, tuple)) or not codes
                or any(not isinstance(code, str) or not code.strip() for code in codes)
                or len({code.strip() for code in codes}) != len(codes)):
            raise ValueError("market codes must be non-empty unique strings")
        result["codes"] = tuple(code.strip() for code in codes)
    if "filters" in value:
        filters = value["filters"]
        if not isinstance(filters, (list, tuple)):
            raise ValueError("market filters must be a list")
        conditions = []
        for condition in filters:
            if not isinstance(condition, Mapping) or set(condition) != {"field", "op", "value"}:
                raise ValueError("market filter must contain field, op and value")
            name, op, expected = condition["field"], condition["op"], condition["value"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("market filter field must be a non-empty string")
            if not isinstance(op, str) or op not in {"eq", "ne", "gt", "gte", "lt", "lte", "in", "between"}:
                raise ValueError("unsupported market filter operator")
            if op == "in":
                if not isinstance(expected, (list, tuple)) or not expected:
                    raise ValueError("market in filter requires a non-empty list")
                for item in expected:
                    finite_scalar(item)
                expected = tuple(expected)
            elif op == "between":
                if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                    raise ValueError("market between filter requires two values")
                for item in expected:
                    finite_scalar(item)
                expected = tuple(expected)
            else:
                finite_scalar(expected)
            conditions.append(MappingProxyType({"field": name.strip(), "op": op, "value": expected}))
        result["filters"] = tuple(conditions)
    return MappingProxyType(result)


def parse_backtest_request(data: JsonValue) -> BacktestRequest:
    if not isinstance(data, dict):
        raise BacktestError("invalid_request", "backtest data must be an object")
    try:
        allowed = {"run_id", "module", "params", "dataset_id", "start", "end", "initial_cash",
                   "fields", "history", "price_basis", "as_of", "market", "limits", "evaluation", "benchmark_id",
                   "execution", "search", "accounting"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unsupported request fields: {', '.join(sorted(unknown))}")
        settlement_parameters = {}
        if "accounting" in data:
            if data['accounting'] not in {'account','research'}:
                raise ValueError('unsupported accounting model')
            settlement_parameters['accounting'] = data['accounting']
        if data.get("limits") is not None:
            settlement_parameters["limits"] = _limits(data["limits"])
        if data.get("execution") is not None:
            settlement_parameters["execution"] = validate_execution(data["execution"])
        if "execution" in settlement_parameters:
            check = settlement_parameters["execution"]["check"]["params"]
            current = settlement_parameters.get("limits")
            if current is None:
                current = _limits({}, lot_size=check["step"])
            else:
                current = _limits({
                    "stock_weight": current.stock_weight,
                    "adv_fraction": current.adv_fraction,
                }, lot_size=check["step"])
            settlement_parameters["limits"] = current
        request = BacktestRequest(
            run_id=_text(data, "run_id") if "run_id" in data else uuid4().hex,
            strategy_id=_text(data, "module"), parameters=_object(data, "params"),
            dataset_id=_text(data, "dataset_id"),
            start=_instant(data, "start", end=False),
            end=_instant(data, "end", end=True),
            initial_cash=_decimal_input(data, "initial_cash"),
            fields=_names(data.get("fields", ()), "fields"), history=data.get("history", 0),
            price_basis=data.get("price_basis", "adjusted"),
            as_of=date.fromisoformat(data["as_of"]) if data.get("as_of") else None,
            market=data.get("market", {"scope": "CN_A"}),
            settlement_parameters=settlement_parameters,
            evaluation_parameters=_object(data, "evaluation") if "evaluation" in data else {},
            benchmark_id=data.get("benchmark_id"),
        )
        return request
    except BacktestError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BacktestError("invalid_request", str(error)) from error


def validate_execution(value: JsonValue) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {"price", "quantity", "fees", "tax", "check"}:
        raise ValueError("execution must declare price, quantity, fees, tax and check")

    def action(name, models, required, optional=()):
        item = value[name]
        if not isinstance(item, dict) or set(item) != {"model", "params"}:
            raise ValueError(f"{name} model and params are required")
        model, params = item["model"], item["params"]
        if model not in models or not isinstance(params, dict):
            raise ValueError(f"unsupported {name} model")
        keys = set(params)
        if not set(required) <= keys or keys - set(required) - set(optional):
            raise ValueError(f"invalid {name} parameters")
        return {"model": model, "params": dict(params)}

    def number(value, name, positive=False):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a number")
        try:
            result = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a number") from error
        if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
            raise ValueError(f"{name} must be finite and valid")
        return result

    price = action("price", {"ratio", "tick", "volume_share"}, (),
                   {"rate", "ticks", "tick", "impact", "capacity_basis"})
    if price["model"] == "ratio" and set(price["params"]) != {"rate", "tick"}:
        raise ValueError("ratio requires rate and tick")
    if price["model"] == "ratio":
        rate = number(price["params"]["rate"], "rate")
        if rate >= 1:
            raise ValueError("rate must be less than one")
        number(price["params"]["tick"], "tick", positive=True)
    if price["model"] == "tick" and set(price["params"]) != {"ticks", "tick"}:
        raise ValueError("tick requires ticks and tick")
    if price["model"] == "tick":
        if type(price["params"]["ticks"]) is not int or price["params"]["ticks"] < 0:
            raise ValueError("ticks must be a nonnegative integer")
        number(price["params"]["tick"], "tick", positive=True)
    if price["model"] == "volume_share" and set(price["params"]) != {"impact", "tick", "capacity_basis"}:
        raise ValueError("volume_share requires impact, tick and capacity_basis")
    if price["model"] == "volume_share" and price["params"]["capacity_basis"] != "prior20":
        raise ValueError("volume_share requires prior20 capacity_basis")
    if price["model"] == "volume_share":
        number(price["params"]["impact"], "impact")
        number(price["params"]["tick"], "tick", positive=True)

    quantity = action("quantity", {"capacity", "participation"}, (),
                      {"participation", "capacity_basis"})
    if quantity["model"] == "capacity" and quantity["params"]:
        raise ValueError("capacity does not accept parameters")
    if quantity["model"] == "participation" and set(quantity["params"]) != {"participation", "capacity_basis"}:
        raise ValueError("participation requires participation and capacity_basis")
    if quantity["model"] == "participation" and quantity["params"]["capacity_basis"] != "prior20":
        raise ValueError("participation requires prior20 capacity_basis")
    if quantity["model"] == "participation":
        participation = number(quantity["params"]["participation"], "participation", positive=True)
        if participation > 1:
            raise ValueError("participation must not exceed one")

    fees = action("fees", {"dated"}, {"rounding"})
    if fees["params"]["rounding"] not in {"none", "half_up"}:
        raise ValueError("fees rounding must be none or half_up")
    tax = action("tax", {"fifo", "none"}, ())
    check = action("check", {"cn_a_share"}, {"tick", "min_qty", "step"},
                   {"limit_up", "limit_down", "limit_source"})
    number(check["params"]["tick"], "tick", positive=True)
    if any(type(check["params"][name]) is not int or check["params"][name] <= 0
           for name in ("min_qty", "step")):
        raise ValueError("min_qty and step must be positive integers")
    bounds = {"limit_up", "limit_down", "limit_source"} & set(check["params"])
    if bounds and bounds != {"limit_up", "limit_down", "limit_source"}:
        raise ValueError("price bounds require limit_up, limit_down and limit_source")
    if bounds:
        lower = number(check["params"]["limit_down"], "limit_down", positive=True)
        upper = number(check["params"]["limit_up"], "limit_up", positive=True)
        if lower > upper or not isinstance(check["params"]["limit_source"], str) or not check["params"]["limit_source"].strip():
            raise ValueError("price bounds require ordered values and a source")
    if number(price["params"]["tick"], "price tick", positive=True) != number(check["params"]["tick"], "check tick", positive=True):
        raise ValueError("price and check ticks must match")
    return {"price": price, "quantity": quantity, "fees": fees, "tax": tax, "check": check}


def _text(data: Mapping[str, JsonValue], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _names(value, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an ordered list")
    return tuple(value)


def _limits(value, *, lot_size=100):
    from core.calculation import AllocationLimits
    if not isinstance(value, dict) or set(value) - {"stock_weight", "adv_fraction"}:
        raise ValueError("unsupported settlement limits")
    defaults = AllocationLimits()
    if type(lot_size) is not int or lot_size <= 0:
        raise ValueError("execution.check step must be a positive integer")
    parsed = {"lot_size": lot_size}
    for name in ("stock_weight", "adv_fraction"):
        raw = value.get(name, getattr(defaults, name))
        if raw is None:
            parsed[name] = None
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{name} must be numeric")
        try:
            number = Decimal(str(raw))
        except (ArithmeticError, ValueError, TypeError) as error:
            raise ValueError(f"{name} must be a finite positive number") from error
        if not number.is_finite() or number <= 0 or number > 1:
            raise ValueError(f"{name} must be a finite number in (0, 1]")
        parsed[name] = number
    return AllocationLimits(**parsed)


def _decimal_input(data: Mapping[str, JsonValue], name: str) -> Decimal:
    value = data[name]
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite positive number")
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite positive number") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _decode_backtest_config(raw: bytes) -> JsonObject:
    """Keep the monetary JSON token exact while preserving ordinary number types."""
    def reject_constant(value):
        raise ValueError(f"non-standard JSON number: {value}")

    decoded = json.loads(raw, parse_float=Decimal, parse_constant=reject_constant)

    def normalize(value, key=None):
        if isinstance(value, Decimal):
            if key == "initial_cash":
                return value
            try:
                number = float(value)
            except OverflowError as error:
                raise ValueError("JSON number is outside the supported range") from error
            if not math.isfinite(number):
                raise ValueError("JSON number is outside the supported range")
            return number
        if isinstance(value, dict):
            return {name: normalize(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return normalize(decoded)


def _identity(data: Mapping[str, JsonValue], name: str) -> str:
    value = _text(data, name)
    if Path(value).name != value:
        raise ValueError(f"{name} must be a data identity")
    return value


def _instant(data: Mapping[str, JsonValue], name: str, *, end: bool = False) -> datetime:
    raw = _text(data, name)
    if len(raw) == 10:
        value = datetime.combine(date.fromisoformat(raw), time.max if end else time.min)
    else:
        value = datetime.fromisoformat(raw)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _config_value(value):
    if is_dataclass(value):
        return {key: _config_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _config_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_config_value(item) for item in value]
    return value


def _strategy_params(strategy, request):
    params = getattr(strategy, "params", None)
    return (_config_value(params) if is_dataclass(params)
            else _config_value(dict(request.parameters)))


def build_effective_config(request, strategy, requirements, dataset, settlement):
    """Build the one serializable config used by preflight, runs and manifests."""
    if is_dataclass(requirements):
        requirements_value = _config_value(asdict(requirements))
    else:
        requirements_value = _config_value(vars(requirements))
    fields = (list(dataset.fields) if dataset is not None
              else list(required_fields(requirements.fields)))
    history = (dataset.history if dataset is not None
               else requirements.history_bars)
    value = {
        "module": request.strategy_id,
        "params": _strategy_params(strategy, request),
        "dataset_id": request.dataset_id,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "initial_cash": str(request.initial_cash),
        "fields": fields,
        "history": history,
        "price_basis": request.price_basis,
        "as_of": request.as_of.isoformat() if request.as_of else None,
        "market": _config_value(request.market),
        "market_resolution": _config_value(dataset.scope.get("market"))
        if dataset is not None else None,
        "requirements": requirements_value,
        "settlement": _config_value(settlement),
        "evaluation": dict(request.evaluation_parameters),
        "benchmark_id": request.benchmark_id,
    }
    from strategy import canonical_hash
    return value, canonical_hash(value)


def _json_value(value):
    """Serialize protocol and settlement dataclasses without a recorder module."""
    from datetime import date as date_type, datetime as datetime_type
    from enum import Enum

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in value.__dataclass_fields__.values()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date_type, datetime_type)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def build_execution_hash(provenance: Mapping[str, object], settlement: object) -> str:
    """Hash source/code evidence together with the actual selected settlement."""
    from strategy import canonical_hash
    return canonical_hash({
        "input": _json_value(provenance),
        "settlement": _config_value(settlement),
    })


def _object(data: Mapping[str, JsonValue], name: str) -> Mapping[str, object]:
    value = data[name]
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


__all__ = ['AccountSnapshot', 'BacktestError', 'BacktestRequest', 'BacktestResponse', 'Bar', 'DataError', 'DataPort', 'DataQuery', 'DataRef', 'DataRequest', 'Dataset', 'EXECUTION_FIELDS', 'Fill', 'HistoryBar', 'HistoryDay', 'HistoryResult', 'JsonObject', 'JsonValue', 'LIQUIDITY_WINDOW', 'MarketFrame', 'Order', 'PROBLEM_SCHEMA', 'PROTOCOL_NAME', 'PROTOCOL_VERSION', 'PortKind', 'RegistrationAction', 'RegistrationRecord', 'Scalar', 'SearchResponse', 'SearchSpec', 'Side', 'TargetSet', 'Trial', 'build_effective_config', 'build_execution_hash', 'normalize_security_id', 'parse_backtest_request', 'required_fields', 'validate_execution', 'validate_facts']
