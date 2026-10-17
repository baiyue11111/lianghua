"""唯一的后端协议入口。

这个文件集中定义外部操作和数据读写协议，把请求分发到模块端口，
把结果转换成 ``frontend/ui-protocol.py`` 的消息。Kernel 只在这里被接入一次。
HTTP、CLI、任务线程和前端组件都不属于本层。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from importlib import import_module
from functools import partial
from inspect import getattr_static, signature
from pathlib import Path
from hashlib import sha256
import hashlib
import re
import json
import os
from threading import local
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol as TypingProtocol, TypeAlias
from uuid import uuid4
from collections import deque
from contextlib import contextmanager
import math
from zoneinfo import ZoneInfo
from typing import Callable
import duckdb
import numpy as np

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
PortKind: TypeAlias = Literal["backtests", "research", "sources", "lake", "commands"]

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
            if any(not isinstance(row, HistoryBar) for row in rows):
                raise TypeError("history rows must be HistoryBar")
            times = tuple(row.at for row in rows)
            if tuple(sorted(set(times))) != times or any(at > self.at for at in times):
                raise ValueError("history must be ordered, unique and not future")
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
    stock_nominal: Decimal
    reserve_taxable: Decimal
    tax_source_sha256: str


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

    def __post_init__(self):
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


class _DataStore(TypingProtocol):
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
    def delete(self, kind: str, identity: str, name: str) -> None: ...
    def manifest(self, dataset: str) -> Mapping[str, object]: ...
    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef: ...
    def update_registry(self, change) -> Mapping[str, object]: ...
    def source_lock(self): ...
    def publish_state(self, dataset: str) -> str | None: ...
    def publish_states(self) -> tuple[str, ...]: ...
    def begin_publish(self, dataset: str, token: str) -> None: ...
    def finish_publish(self, dataset: str, token: str) -> None: ...


class DataPort(TypingProtocol):
    """The single public data bus contract used by module consumers."""

    def query(self, query: DataQuery) -> Dataset: ...
    def read_ref(self, ref: DataRef) -> bytes: ...
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
    def delete(self, kind: str, identity: str, name: str) -> None: ...
    def manifest(self, dataset: str) -> Mapping[str, object]: ...
    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef: ...
    def transaction(self, namespace: str): ...
    def lake_facts(self) -> Mapping[str, object]: ...
    def profile_report(self) -> Mapping[str, object]: ...
    def research_report(self) -> Mapping[str, object]: ...
    def doctor_report(self) -> Mapping[str, object]: ...


class DataBus:
    """All business persistence enters this standard protocol boundary."""
    kinds = frozenset({'sources', 'runs', 'state', 'validation'})

    def __init__(self, store: _DataStore):
        self._store = store
        self._publish_tokens = local()
        self._transactions = local()
        self._pid = os.getpid()

    def _process(self):
        if self._pid != os.getpid():
            self._publish_tokens = local()
            self._transactions = local()
            self._pid = os.getpid()

    @contextmanager
    def transaction(self, namespace):
        self._process()
        if namespace not in self.kinds:
            raise DataError('invalid_identity', 'invalid transaction namespace')
        active = getattr(self._transactions, 'values', {})
        self._transactions.values = active
        if namespace in active:
            try:
                yield self
            except BaseException:
                active[namespace]['failed'] = True
                raise
            return
        with self._store.namespace_lock(namespace):
            if self._store.publish_states(namespace):
                raise DataError('dataset_publishing', 'namespace publication is incomplete')
            state = {'token': uuid4().hex, 'started': False, 'failed': False}
            active[namespace] = state
            try:
                yield self
            except BaseException:
                raise
            else:
                if state['failed']:
                    raise DataError('dataset_publishing', 'nested transaction failed')
                if state['started']:
                    self._store.finish_publish('_transaction', state['token'], namespace)
            finally:
                active.pop(namespace, None)
                self._publish_owner().pop('_transaction', None)

    def _transaction_write(self, namespace):
        state = getattr(self._transactions, 'values', {}).get(namespace)
        if state is not None and not state['started']:
            self._store.begin_publish('_transaction', state['token'], namespace)
            state['started'] = True
            if namespace == 'sources':
                self._publish_owner()['_transaction'] = state['token']

    def _publish_owner(self) -> dict[str, str]:
        self._process()
        values = getattr(self._publish_tokens, "values", None)
        if values is None:
            values = {}
            self._publish_tokens.values = values
        return values

    def _source_owner_allowed(self) -> None:
        states = self._store.publish_states()
        owners = self._publish_owner()
        if states and not any(token in owners.values() for token in states):
            raise DataError("dataset_publishing", "dataset publication belongs to another writer")

    def _source_mutate(self, operation, identity, *args):
        with self._store.source_lock():
            self._source_owner_allowed()
            self._call('path', 'sources', identity, args[0])
            if operation == 'write' and not isinstance(args[1], bytes):
                raise DataError('invalid_identity', 'data value must be bytes')
            self._transaction_write('sources')
            try:
                return self._call(operation, "sources", identity, *args)
            except BaseException:
                state = getattr(self._transactions, 'values', {}).get('sources')
                if state is not None:
                    state['failed'] = True
                raise

    def _source_view(self, operation, identity, *args):
        with self._store.source_lock():
            self._source_owner_allowed()
            return self._call(operation, "sources", identity, *args)

    def _namespace_access(self, operation, namespace, *args):
        if namespace not in self.kinds:
            raise DataError('invalid_identity', 'invalid data kind')
        with self._store.namespace_lock(namespace):
            states = self._store.publish_states(namespace)
            state = getattr(self._transactions, 'values', {}).get(namespace)
            if states and (state is None or state['token'] not in states):
                raise DataError('dataset_publishing', 'namespace publication is incomplete')
            if operation in ('write', 'delete'):
                self._call('path', namespace, args[0], args[1])
                if operation == 'write' and not isinstance(args[2], bytes):
                    raise DataError('invalid_identity', 'data value must be bytes')
                self._transaction_write(namespace)
            try:
                return self._call(operation, namespace, *args)
            except BaseException:
                if state is not None and operation in ('write', 'delete'):
                    state['failed'] = True
                raise

    @staticmethod
    def _sql_name(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _market_storage_ids(self) -> tuple[str, ...]:
        try:
            registry = json.loads(self._call("read", "sources", "registry", "sources.json"))
        except (DataError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise DataError("invalid_registry", "market storage is not registered") from error
        markets = registry.get("markets") if isinstance(registry, Mapping) else None
        datasets = registry.get("datasets") if isinstance(registry, Mapping) else None
        if not isinstance(markets, list) or not isinstance(datasets, list):
            raise DataError("invalid_registry", "market storage is not registered")
        descriptors = {row.get("id"): row for row in datasets
                       if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
        market_ids = {row.get("dataset_id") for row in markets
                      if isinstance(row, Mapping) and isinstance(row.get("dataset_id"), str)}
        if not market_ids:
            raise DataError("invalid_registry", "market storage is not registered")
        storage_ids = set()
        for dataset_id in market_ids:
            descriptor = descriptors.get(dataset_id)
            if descriptor is None:
                raise DataError("invalid_registry", "market dataset is not registered")
            storage_id = descriptor.get("storage_id", dataset_id)
            if not isinstance(storage_id, str) or not storage_id or Path(storage_id).name != storage_id:
                raise DataError("invalid_registry", "market storage identity is invalid")
            storage_ids.add(storage_id)
        return tuple(sorted(storage_ids))

    def _lake_files(self, suffix: str) -> list[tuple[str, Path]]:
        return [(f"{identity}/{ref.name}", self._store.path("sources", identity, ref.name))
                for identity in self._market_storage_ids()
                for ref in self._store.list("sources", identity)
                if ref.name.endswith(suffix) and ref.name.endswith(".parquet")]

    def _lake_query(self, files: list[tuple[str, Path]], sql: str):
        if not files:
            return None
        paths = [str(path) for _, path in files]
        connection = duckdb.connect(":memory:")
        try:
            return connection.execute(sql, [paths]).fetchall()
        finally:
            connection.close()

    def _lake_columns(self, files: list[tuple[str, Path]]) -> set[str]:
        if not files:
            return set()
        paths = [str(path) for _, path in files]
        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute(
                "describe select * from read_parquet(?, union_by_name=true)", [paths]
            ).fetchall()
            return {str(row[0]) for row in rows}
        finally:
            connection.close()

    def _lake_table_facts(self, files: list[tuple[str, Path]], *, date_field: str = "date",
                          with_fields: bool = True) -> dict:
        if not files:
            return {"rows": 0, "symbols": 0, "first_date": None, "last_date": None,
                    "columns": set(), "field_stats": {}, "missing": True}
        columns = self._lake_columns(files)
        paths = [str(path) for _, path in files]
        connection = duckdb.connect(":memory:")
        try:
            source = "read_parquet(?, union_by_name=true)"
            code = self._sql_name("code") if "code" in columns else "NULL"
            dates = self._sql_name(date_field) if date_field in columns else "NULL"
            row = connection.execute(
                f"select count(*), count(distinct {code}), min({dates}), max({dates}) from {source}",
                [paths],
            ).fetchone()
            field_stats = {}
            if columns and with_fields:
                expressions = []
                for index, name in enumerate(sorted(columns)):
                    quoted = self._sql_name(name)
                    expressions.extend((
                        f"count({quoted}) as non_null_{index}",
                        f"100.0 * avg(case when {quoted} is not null then 1.0 else 0.0 end) as coverage_{index}",
                    ))
                    if "code" in columns:
                        expressions.append(
                            f"count(distinct case when {quoted} is not null then {self._sql_name('code')} end) "
                            f"as symbols_{index}"
                        )
                values = connection.execute(
                    f"select {', '.join(expressions)} from {source}", [paths]
                ).fetchone()
                width = 3 if "code" in columns else 2
                for index, name in enumerate(sorted(columns)):
                    offset = index * width
                    field_stats[name] = {
                        "non_null": int(values[offset] or 0),
                        "coverage": None if values[offset + 1] is None else float(values[offset + 1]),
                        "symbols": int(values[offset + 2] or 0) if width == 3 else None,
                    }
            return {"rows": int(row[0] or 0), "symbols": int(row[1] or 0),
                    "first_date": None if row[2] is None else str(row[2]),
                    "last_date": None if row[3] is None else str(row[3]),
                    "columns": columns, "field_stats": field_stats, "missing": False}
        finally:
            connection.close()

    def _daily_fact(self, files: list[tuple[str, Path]]) -> dict:
        fact = self._lake_table_facts(files, with_fields=False)
        if not files:
            fact.update({"duplicate_keys": 0, "invalid_price_rows": 0,
                         "missing_fields": {field: 0 for field in EXECUTION_FIELDS},
                         "field_coverage": {field: None for field in EXECUTION_FIELDS},
                         "field_symbols": {field: 0 for field in EXECUTION_FIELDS},
                         "adjusted_close_rows": 0,
                         "trade_state_issues": {"suspended_with_turnover": 0,
                                                "trading_without_turnover": 0,
                                                "unknown_zero_turnover": 0},
                         "latest_stock_count": 0, "tracked_stock_count": 0,
                         "median_stock_date": None, "yearly": {},
                         "price_basis_issues": []})
            return fact
        paths = [str(path) for _, path in files]
        columns = fact["columns"]
        connection = duckdb.connect(":memory:")
        try:
            source = "read_parquet(?, union_by_name=true)"
            duplicate = connection.execute(
                f"select count(*) - count(distinct ({self._sql_name('code')}, {self._sql_name('date')})) "
                f"from {source}", [paths]).fetchone()[0]
            aggregate = []
            for field in EXECUTION_FIELDS:
                name = self._sql_name(field) if field in columns else "NULL"
                aggregate.extend((
                    f"count({name})", f"100.0 * avg(case when {name} is not null then 1.0 else 0.0 end)",
                    f"count(distinct case when {name} is not null then {self._sql_name('code')} end)",
                ))
            open_name = self._sql_name("open") if "open" in columns else "NULL"
            high_name = self._sql_name("high") if "high" in columns else "NULL"
            low_name = self._sql_name("low") if "low" in columns else "NULL"
            close_name = self._sql_name("close") if "close" in columns else "NULL"
            invalid_expression = (
                f"sum(case when {open_name} is null or {high_name} is null or {low_name} is null "
                f"or {close_name} is null or {open_name} <= 0 or {high_name} <= 0 "
                f"or {low_name} <= 0 or {close_name} <= 0 or {high_name} < {low_name} then 1 else 0 end)"
                if {"open", "high", "low", "close"} <= columns else "0"
            )
            aggregate.append(invalid_expression)
            status = self._sql_name("tradestatus") if "tradestatus" in columns else "NULL"
            volume = self._sql_name("volume") if "volume" in columns else "NULL"
            amount = self._sql_name("amount") if "amount" in columns else "NULL"
            aggregate.extend((
                f"sum(case when {status}=0 and (coalesce({volume},0)>0 or coalesce({amount},0)>0) then 1 else 0 end)",
                f"sum(case when {status}=1 and (coalesce({volume},0)<=0 or coalesce({amount},0)<=0) then 1 else 0 end)",
                f"sum(case when {status} is null and coalesce({volume},0)=0 and coalesce({amount},0)=0 then 1 else 0 end)",
                f"count({self._sql_name('close_hfq') if 'close_hfq' in columns else 'NULL'})",
            ))
            aggregate_row = connection.execute(f"select {', '.join(aggregate)} from {source}", [paths]).fetchone()
            missing_fields, coverage, field_symbols = {}, {}, {}
            offset = 0
            for field in EXECUTION_FIELDS:
                if field not in columns:
                    missing_fields[field], coverage[field], field_symbols[field] = fact["rows"], None, 0
                else:
                    missing_fields[field] = fact["rows"] - int(aggregate_row[offset] or 0)
                    coverage[field] = None if aggregate_row[offset + 1] is None else float(aggregate_row[offset + 1])
                    field_symbols[field] = int(aggregate_row[offset + 2] or 0)
                offset += 3
            invalid = int(aggregate_row[offset] or 0)
            issues = {"suspended_with_turnover": 0, "trading_without_turnover": 0,
                      "unknown_zero_turnover": 0}
            if "tradestatus" in columns and {"volume", "amount"} <= columns:
                issues = {key: int(value or 0) for key, value in zip(issues, aggregate_row[offset + 1:offset + 4])}
            adjusted = int(aggregate_row[offset + 4] or 0) if "close_hfq" in columns else 0
            latest = connection.execute(
                f"with latest as (select {self._sql_name('code')} code, max(cast({self._sql_name('date')} as date)) last_date "
                f"from {source} group by {self._sql_name('code')}) "
                f"select max(last_date), count(*), count(*) filter (where last_date=(select max(last_date) from latest)), "
                f"cast(median(last_date) as date) from latest", [paths]).fetchone()
            basis_rows = []
            if {"code", "date", "amount", "volume", "low", "high", "close"} <= columns:
                basis_rows = connection.execute(
                    f"select {self._sql_name('code')}, count(*), min({self._sql_name('date')}), "
                    f"max({self._sql_name('date')}), median({self._sql_name('amount')}/{self._sql_name('volume')}/{self._sql_name('close')}) "
                    f"from {source} where {self._sql_name('volume')}>0 and {self._sql_name('amount')}>0 "
                    f"and {self._sql_name('low')}>0 and {self._sql_name('high')}>0 and {self._sql_name('close')}>0 "
                    f"and ({self._sql_name('amount')}/{self._sql_name('volume')} < {self._sql_name('low')}*0.99 "
                    f"or {self._sql_name('amount')}/{self._sql_name('volume')} > {self._sql_name('high')}*1.01) "
                    f"group by {self._sql_name('code')} order by 2 desc, 1", [paths]).fetchall()
            yearly_rows = connection.execute(
                f"select year(cast({self._sql_name('date')} as date)), count(*), count(distinct {self._sql_name('code')}) "
                f"from {source} group by 1 order by 1", [paths]).fetchall()
            adjusted = 0
            if "close_hfq" in columns:
                adjusted = int(connection.execute(
                    f"select count({self._sql_name('close_hfq')}) from {source}", [paths]
                ).fetchone()[0] or 0)
            fact.update({"duplicate_keys": int(duplicate or 0), "invalid_price_rows": int(invalid or 0),
                         "missing_fields": missing_fields, "field_coverage": coverage,
                         "field_symbols": field_symbols, "adjusted_close_rows": adjusted,
                         "trade_state_issues": issues,
                         "latest_date": None if latest[0] is None else str(latest[0]),
                         "tracked_stock_count": int(latest[1] or 0),
                         "latest_stock_count": int(latest[2] or 0),
                         "median_stock_date": None if latest[3] is None else str(latest[3]),
                         "yearly": {str(row[0]): {"rows": int(row[1]), "symbols": int(row[2])}
                                    for row in yearly_rows},
                         "price_basis_issues": [
                             {"code": row[0], "rows": int(row[1]), "first_date": str(row[2]),
                              "last_date": str(row[3]), "median_vwap_close_ratio": float(row[4])}
                             for row in basis_rows]})
            return fact
        finally:
            connection.close()

    def lake_facts(self) -> Mapping[str, object]:
        """Aggregate current lake facts without using a report or release manifest."""
        with self._store.source_lock():
            self._source_owner_allowed()
            daily_files = self._lake_files(".parquet")
            daily_files = [(name, path) for name, path in daily_files
                           if Path(name).parent.name == "daily"]
            tables = {
                "stock_info": self._lake_files("stock_info.parquet"),
                "valuation": self._lake_files("valuation_factors.parquet"),
                "fundamentals": self._lake_files("fundamentals.parquet"),
            }
            lake_refs = [(f"{identity}/{ref.name}", ref) for identity in self._market_storage_ids()
                         for ref in self._store.list("sources", identity)]
            facts = {name: self._lake_table_facts(files, date_field="quarter")
                     for name, files in tables.items()}
            for name, files in tables.items():
                if name == "stock_info" and files:
                    columns = facts[name]["columns"]
                    if "status" in columns:
                        paths = [str(path) for _, path in files]
                        connection = duckdb.connect(":memory:")
                        try:
                            row = connection.execute(
                                "select sum(case when lower(cast(status as varchar)) in ('listed','现存','true') then 1 else 0 end), "
                                "sum(case when lower(cast(status as varchar)) not in ('listed','现存','true') or status is null then 1 else 0 end) "
                                "from read_parquet(?, union_by_name=true)", [paths]).fetchone()
                            facts[name]["listed"] = int(row[0] or 0)
                            facts[name]["delisted_or_unknown"] = int(row[1] or 0)
                        finally:
                            connection.close()
            fact_names = sorted(ref.name for ref in self._store.list("sources", "facts")
                                if ref.name.startswith(("adjust_", "dividend_", "stock_basic_")) and ref.name.endswith(".json"))
            sample_names = (fact_names[:8] + fact_names[-8:]
                            if len(fact_names) > 16 else fact_names)
            fact_valid = fact_invalid = 0
            for name in sample_names:
                try:
                    value = json.loads(self._store.read("sources", "facts", name))
                    kind = value.get("kind") if isinstance(value, Mapping) else None
                    code = value.get("code") if isinstance(value, Mapping) else None
                    if kind not in {"adjust", "dividend", "security"} or not isinstance(code, str):
                        raise ValueError("standard fact kind or code is invalid")
                    start, end, year = (value.get("start"), value.get("end"), value.get("year"))
                    validate_facts(value, kind=kind, code=code,
                                    start=start, end=end, year=year)
                    fact_valid += 1
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    fact_invalid += 1
            code_changes = []
            code_change_status = "unknown"
            try:
                value = json.loads(self._store.read(
                    "sources", "facts", "security_code_changes.json"))
                events = value.get("events", []) if isinstance(value, Mapping) else []
                code_changes = [event for event in events
                                if isinstance(event, Mapping)
                                and event.get("factor_basis_continuity_verified") is not True]
                code_change_status = "checked"
            except DataError as error:
                if error.code != "missing":
                    raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                code_change_status = "invalid"
            daily = self._daily_fact(daily_files)
            errors = []
            if not daily_files:
                errors.append("日线行情文件不存在")
            for field, count in daily["missing_fields"].items():
                if count:
                    errors.append(f"日线字段 {field} 缺失 {count} 行")
            if daily["duplicate_keys"]:
                errors.append(f"日线重复键 {daily['duplicate_keys']} 行")
            if daily["invalid_price_rows"]:
                errors.append(f"日线价格异常 {daily['invalid_price_rows']} 行")
            if daily["price_basis_issues"]:
                errors.append(f"成交额与价格口径异常 {len(daily['price_basis_issues'])} 个代码")
            for name, label in (("stock_info", "股票主档"), ("valuation", "估值因子"),
                                ("fundamentals", "基本面因子")):
                if facts[name]["missing"]:
                    errors.append(f"{label}事实表不存在")
            for key, count in daily["trade_state_issues"].items():
                if count:
                    errors.append(f"交易状态 {key} {count} 行")
            if code_change_status != "checked":
                errors.append(f"代码变更事实{code_change_status}，未完成检查")
            if fact_invalid:
                errors.append(f"标准事实抽样无效 {fact_invalid} 个")
            if len(sample_names) < len(fact_names):
                errors.append(f"标准事实仅抽样检查 {len(sample_names)}/{len(fact_names)} 个")
            return {"daily": daily, **facts, "errors": errors,
                    "partition_sha256": {name: ref.sha256 for name, ref in lake_refs
                                         if ref.name.startswith("daily/") and ref.name.endswith(".parquet")},
                    "saved_factor_files": sum(1 for name in fact_names if name.startswith("adjust_")),
                    "facts": {"files": len(fact_names), "checked": len(sample_names),
                                        "valid": fact_valid, "invalid": fact_invalid,
                                        "check_scope": "sample" if len(sample_names) < len(fact_names)
                                        else "all"},
                    "unverified_code_changes": code_changes,
                    "code_change_status": code_change_status}

    def search(self, query: str) -> Mapping[str, object]:
        """Search canonical security metadata and its latest market fact."""
        if not isinstance(query, str):
            raise TypeError("q must be text")
        with self._store.source_lock():
            self._source_owner_allowed()
            stock_files = self._lake_files("stock_info.parquet")
            latest_files = self._lake_files("latest_snapshot.parquet")
            if not stock_files:
                raise DataError("missing", "canonical stock_info table is required")
            stock_paths = [str(path) for _, path in stock_files]
            latest_paths = [str(path) for _, path in latest_files]
            latest_sql = (
                "read_parquet(?, union_by_name=true)"
                if latest_paths else
                "(select cast(null as varchar) as code, cast(null as varchar) as date, "
                "cast(null as double) as close, cast(null as double) as pctChg, "
                "cast(null as double) as amount where false)"
            )
            connection = duckdb.connect(":memory:")
            try:
                rows = connection.execute(
                    """
                    select s.code, s.name, s.exchange, s.status, s.first_date, s.last_date,
                           l.date, l.close, l.pctChg, l.amount
                    from read_parquet(?, union_by_name=true) s
                    left join """ + latest_sql + """ l
                      on cast(l.code as varchar) = cast(s.code as varchar)
                    where cast(s.code as varchar) like '%' || ? || '%'
                       or coalesce(cast(s.name as varchar), '') like '%' || ? || '%'
                    order by s.code
                    limit 20
                    """,
                    [stock_paths, *([latest_paths] if latest_paths else []), query, query],
                ).fetchall()
            finally:
                connection.close()
        items = []
        for code, name, exchange, status, first_date, last_date, market_date, close, pct_chg, amount in rows:
            if status in {"listed", "现存", True}:
                status = "现存"
            elif status is not None:
                status = "退市"
            items.append({"代码": code, "名称": name, "交易所": exchange,
                          "状态": status, "首日": first_date, "末日": last_date,
                          "最新收盘": close, "涨跌幅": pct_chg,
                          "成交额亿": None if amount is None else amount / 100000000,
                          "行情日期": market_date})
        return {"items": items}

    def profile_report(self) -> Mapping[str, object]:
        facts = self.lake_facts()
        daily = facts["daily"]
        first, last = daily.get("first_date"), daily.get("last_date")
        interval = "" if first is None or last is None else f"{first} ~ {last}"
        overview = {
            "日线行情": {"行数": daily["rows"], "股票数": daily["symbols"],
                         "区间": interval, "落后交易日": None},
            "股票主档": {"行数": facts["stock_info"]["rows"],
                         "现存": facts["stock_info"].get("listed"),
                         "退市": facts["stock_info"].get("delisted_or_unknown")},
            "估值因子": {"行数": facts["valuation"]["rows"], "股票数": facts["valuation"]["symbols"],
                         "区间": f"{facts['valuation'].get('first_date')} ~ {facts['valuation'].get('last_date')}"},
            "基本面因子": {"行数": facts["fundamentals"]["rows"], "股票数": facts["fundamentals"]["symbols"],
                           "区间": f"{facts['fundamentals'].get('first_date')} ~ {facts['fundamentals'].get('last_date')}"}
        }
        fields = {field: {"有值率": daily["field_coverage"].get(field),
                          "覆盖股数": daily["field_symbols"].get(field)}
                  for field in EXECUTION_FIELDS}
        complete = not facts["errors"] and daily["rows"] > 0
        return {"生成时间": datetime.now().isoformat(), "总览": overview,
                "年度覆盖": daily["yearly"], "日线字段": fields,
                "估值因子字段": facts["valuation"]["field_stats"],
                "基本面因子字段": facts["fundamentals"]["field_stats"],
                "异常检查": facts["errors"],
                "回测区间": {"区间": interval, "行数": daily["rows"],
                             "股票数": daily["symbols"], "完整": complete},
                "结论": {"级别": "✅" if complete else "⚠️",
                         "要点": facts["errors"], "回测可用": complete}}

    def research_report(self) -> Mapping[str, object]:
        facts = self.lake_facts()
        daily = facts["daily"]
        return {"summary": {"first_date": daily.get("first_date") or "",
                             "last_date": daily.get("last_date") or "",
                             "rows": daily["rows"], "symbols": daily["symbols"],
                             "adjusted_close_rows": daily["adjusted_close_rows"],
                             "invalid_price_rows": daily["invalid_price_rows"]},
                "duplicate_keys": daily["duplicate_keys"],
                "price_basis_issues": daily["price_basis_issues"],
                "trade_state_issues": daily["trade_state_issues"],
                "freshness": {"latest_date": daily.get("latest_date"),
                              "latest_stock_count": daily["latest_stock_count"],
                              "tracked_stock_count": daily["tracked_stock_count"],
                              "median_stock_date": daily.get("median_stock_date")},
                "unverified_code_changes": (facts["unverified_code_changes"]
                                             if facts["code_change_status"] == "checked"
                                             else [{"status": facts["code_change_status"]}]),
                "saved_factor_files": facts["saved_factor_files"],
                "fact_check": facts["facts"],
                "partition_sha256": facts["partition_sha256"], "ranking_status": "NOT_RELEASED",
                "requirements_before_ranking": list(facts["errors"])}

    def doctor_report(self) -> Mapping[str, object]:
        facts = self.lake_facts()
        try:
            registry = json.loads(self._call("read", "sources", "registry", "sources.json"))
        except DataError as error:
            if error.code != "missing":
                raise
            registry = {}
        rows = registry.get("sources", []) if isinstance(registry, Mapping) else []
        def repair_source(data_type):
            matches = [row for row in rows if isinstance(row, Mapping)
                       and row.get("data_type") == data_type
                       and row.get("default") is True
                       and set(row.get("modes", ())) & {"fix", "doctor"}]
            return dict(matches[0]) if len(matches) == 1 else None
        items = []
        for index, issue in enumerate(facts["errors"], 1):
            data_type = None
            if issue.startswith("日线") or issue.startswith("成交额"):
                data_type = "daily"
            elif issue.startswith("股票主档"):
                data_type = "stock_info"
            elif issue.startswith("估值因子"):
                data_type = "valuation"
            elif issue.startswith("基本面因子"):
                data_type = "fundamentals"
            source = repair_source(data_type) if data_type else None
            fix = None if source is None else {
                "source_id": source["id"], "mode": "fix",
                "data_type": data_type, "force": False,
            }
            items.append({"id": f"fact:{index}", "级别": "error", "问题": issue,
                          "可修复": fix is not None, "fix": fix,
                          "动作": "按登记源补齐" if fix else "无唯一登记源",
                          "范围": data_type or "lake",
                          "说明": "由登记源执行同一 Collect 流程" if fix
                          else "当前没有可安全选择的登记源，不能猜测修复"})
        return {"items": items}

    def query(self, query: DataQuery) -> Dataset:
        if not isinstance(query, DataQuery):
            raise DataError("invalid_query", "query must be DataQuery")
        with self._store.source_lock():
            if self._store.publish_states():
                raise DataError("dataset_publishing", "dataset publication is incomplete")
            return self._query_locked(query)

    def _query_locked(self, query: DataQuery) -> Dataset:
        if not isinstance(query, DataQuery):
            raise DataError("invalid_query", "query must be DataQuery")
        try:
            source_query = query
            if query.market is not None:
                from dataclasses import replace
                from plugins.data import Adapter
                candidates = Adapter.candidates(self, query.market, query.dataset,
                                                query.start, query.end)
                if not candidates:
                    raise DataError("market_data_missing", "market selector has no source candidates")
                source_query = replace(query, symbols=candidates)
            result = _prepare_dataset(self, source_query)
            if query.market is not None:
                from plugins.data import Adapter
                result = Adapter.market(self, query, result)
        except DataError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise DataError("invalid_query", str(error)) from error
        if not isinstance(result, Dataset):
            raise DataError("invalid_dataset", "data query did not return Dataset")
        if result.dataset != query.dataset:
            raise DataError("dataset_mismatch", "query returned a different dataset")
        if result.scope.get("universe") is None:
            raise DataError("invalid_dataset", "dataset scope must declare universe")
        if query.universe != "dataset" and result.scope.get("universe") != query.universe:
            raise DataError("scope_mismatch", "dataset universe does not satisfy query")
        if query.symbols is not None and tuple(result.scope.get("symbols", ())) != query.symbols:
            raise DataError("scope_mismatch", "dataset symbols do not satisfy query")
        coverage_start = result.scope.get("calendar_start")
        if not isinstance(coverage_start, str) or date.fromisoformat(coverage_start) > query.start:
            raise DataError("date_coverage", "dataset calendar does not cover query start")
        if tuple(result.fields) != tuple(query.fields):
            raise DataError("field_mismatch", "dataset fields do not satisfy query")
        if result.history != query.history:
            raise DataError("history_mismatch", "dataset history does not satisfy query")
        if not result.records[-1].trade_date <= query.end < result.calendar[-1]:
            raise DataError("date_coverage", "dataset must cover the query end before its following-session sentinel")
        return result

    def manifest(self, dataset: str) -> Mapping[str, object]:
        if not isinstance(dataset, str) or Path(dataset).name != dataset or not dataset:
            raise DataError("invalid_identity", "invalid dataset identity")
        with self._store.source_lock():
            self._source_owner_allowed()
            return self._manifest_locked(dataset)

    def _manifest_locked(self, dataset: str) -> Mapping[str, object]:
        """Read one registered dataset manifest after checking its evidence."""
        if not isinstance(dataset, str) or Path(dataset).name != dataset or not dataset:
            raise DataError("invalid_identity", "invalid dataset identity")
        try:
            manifest = json.loads(self.read("sources", dataset, "manifest.json"))
        except DataError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise DataError("invalid_manifest", str(error)) from error
        self._validate_manifest(dataset, manifest)
        return manifest

    def _manifest_file(self, dataset: str, name: str) -> bytes:
        parts = Path(name).parts
        if parts and parts[0] == "source":
            if len(parts) < 3 or any(part in ("", ".", "..") for part in parts):
                raise DataError("invalid_manifest", f"invalid shared source reference: {name}")
            return self.read("sources", parts[1], Path(*parts[2:]).as_posix())
        return self.read("sources", dataset, name)

    def read_ref(self, ref: DataRef) -> bytes:
        """Read and verify one explicit source reference through the bus."""
        if not isinstance(ref, DataRef):
            raise DataError("invalid_ref", "data reference is required")
        if ref.kind not in self.kinds or not ref.identity or not ref.name:
            raise DataError("invalid_ref", "data reference identity is invalid")
        expected_file = self._store.path(ref.kind, ref.identity, ref.name)
        expected_file = expected_file.relative_to(self._store.project).as_posix()
        if ref.file != expected_file:
            raise DataError("invalid_ref", "data reference file does not match its identity")
        value = self.read(ref.kind, ref.identity, ref.name)
        if len(value) != ref.size or sha256(value).hexdigest() != ref.sha256:
            raise DataError("source_hash_mismatch", f"data reference hash mismatch: {ref.file}")
        return value

    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef:
        """Validate all bound evidence, then publish the manifest as the release marker."""
        owners = self._publish_owner()
        token = owners.get(dataset)
        with self._store.source_lock():
            states = self._store.publish_states()
            state = self._store.publish_state(dataset)
            if states and state != token:
                raise DataError("dataset_publishing", "another dataset publication is incomplete")
            if state is not None and state != token:
                raise DataError("dataset_publishing", "dataset publication belongs to another writer")
            temporary = state is None
            if temporary:
                token = uuid4().hex
                self._store.begin_publish(dataset, token)
                owners[dataset] = token
            try:
                result = self._publish_locked(dataset, manifest)
            except Exception:
                if temporary:
                    self._store.finish_publish(dataset, token)
                    owners.pop(dataset, None)
                raise
            self._store.finish_publish(dataset, token)
            owners.pop(dataset, None)
            return result

    def update_registry(self, change):
        with self._store.source_lock():
            self._source_owner_allowed()
            try:
                value = json.loads(self._call("read", "sources", "registry", "sources.json"))
            except DataError as error:
                if error.code != "missing":
                    raise
                value = {"sources": [], "datasets": [], "benchmarks": []}
            if not isinstance(value, dict):
                raise DataError("invalid_registry", "source registry must be an object")
            updated = change(json.loads(json.dumps(value, ensure_ascii=False)))
            if not isinstance(updated, Mapping):
                raise DataError("invalid_registry", "registry update must return an object")
            content = json.dumps(dict(updated), ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode()
            self._call("write", "sources", "registry", "sources.json", content)
            return dict(updated)

    def _publish_locked(self, dataset: str, manifest: Mapping[str, object]) -> DataRef:
        """Publish while the dataset range lock is held."""
        self._validate_manifest(dataset, manifest)
        content = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode()
        return self.write("sources", dataset, "manifest.json", content)

    def begin_publish(self, dataset: str) -> None:
        token = uuid4().hex
        with self._store.source_lock():
            if self._store.publish_states():
                raise DataError("dataset_publishing", "another dataset publication is incomplete")
            self._store.begin_publish(dataset, token)
        self._publish_owner()[dataset] = token

    def _validate_manifest(self, dataset: str, manifest: Mapping[str, object]) -> None:
        if not isinstance(dataset, str) or not dataset or Path(dataset).name != dataset:
            raise DataError("invalid_identity", "invalid dataset identity")
        if not isinstance(manifest, Mapping) or manifest.get("schema") != 2:
            raise DataError("invalid_manifest", "unsupported dataset manifest schema")
        scope = manifest.get("scope")
        if not isinstance(scope, Mapping):
            raise DataError("invalid_manifest", "dataset scope is required")
        symbols = scope.get("symbols")
        universe = scope.get("universe")
        if (not isinstance(universe, str) or not universe.strip()
                or not isinstance(symbols, (list, tuple)) or not symbols
                or tuple(sorted(set(symbols))) != tuple(symbols)
                or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)):
            raise DataError("invalid_manifest", "dataset scope must declare ordered unique symbols and universe")
        fields = manifest.get("fields")
        if not isinstance(fields, Mapping) or len(set(fields)) != len(fields):
            raise DataError("invalid_manifest", "dataset fields must be a mapping")
        if any(not isinstance(name, str) or not name.strip() for name in fields):
            raise DataError("invalid_manifest", "dataset field names must be non-empty strings")
        refs = manifest.get("files")
        if not isinstance(refs, list) or not refs:
            raise DataError("invalid_manifest", "dataset evidence files are required")
        names = set()
        for ref in refs:
            if not isinstance(ref, Mapping) or not {"file", "sha256"} <= set(ref):
                raise DataError("invalid_manifest", "dataset references must declare file and sha256")
            name, digest = ref["file"], ref["sha256"]
            if (not isinstance(name, str) or not name or Path(name).is_absolute()
                    or ".." in Path(name).parts or name in names):
                raise DataError("invalid_manifest", "dataset evidence files must be relative and unique")
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    ):
                raise DataError("invalid_manifest", f"invalid dataset evidence reference: {name}")
            names.add(name)
            try:
                value = self._manifest_file(dataset, name)
            except DataError as error:
                raise DataError("invalid_manifest", f"unbound dataset evidence: {name}") from error

    def _call(self, operation, kind, *args):
        try:
            if kind not in self.kinds:
                raise ValueError('invalid data kind')
            return getattr(self._store, operation)(kind, *args)
        except FileNotFoundError as error:
            raise DataError('missing', str(error)) from error
        except (ValueError, TypeError) as error:
            raise DataError('invalid_identity', str(error)) from error
        except OSError as error:
            raise DataError('io_failed', str(error)) from error

    def read(self, kind, identity, name) -> bytes:
        if kind == "sources":
            return self._source_view('read', identity, name)
        return self._namespace_access('read', kind, identity, name)

    def write(self, kind, identity, name, value: bytes) -> DataRef:
        if kind == "sources":
            return self._source_mutate('write', identity, name, value)
        return self._namespace_access('write', kind, identity, name, value)

    def list(self, kind, identity=None) -> tuple[DataRef, ...]:
        if kind == "sources":
            return self._source_view('list', identity)
        return self._namespace_access('list', kind, identity)

    def path(self, kind, identity, name) -> Path:
        if kind == "sources":
            return self._source_view('path', identity, name)
        return self._namespace_access('path', kind, identity, name)

    def delete(self, kind, identity, name) -> None:
        if kind == "sources":
            self._source_mutate('delete', identity, name)
            return
        self._namespace_access('delete', kind, identity, name)


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
def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, read: Callable[[Path], bytes]) -> str:
    return hashlib.sha256(read(path)).hexdigest()


def _factor_number(raw: object, field: str) -> float:
    if not isinstance(raw, str) or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
        raise ValueError(f"invalid {field}: {raw!r}")
    try:
        value = float(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field}: {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"non-positive or non-finite {field}: {raw!r}")
    return value


def _load_factor_cache(path: Path, symbol: str, start: date,
                       end: date, read: Callable[[Path], bytes]) -> tuple[tuple[FactorEvent, ...], SourceHash]:
    from core.calculation import FactorEvent, SourceHash
    raw_file = read(path)
    payload = json.loads(raw_file)
    code = normalize_security_id(symbol)
    rows, _ = validate_facts(payload, kind="adjust", code=code,
                              start=start.isoformat(), end=end.isoformat(), year=None)
    events = []
    last_event = None
    for row in rows:
        effective = date.fromisoformat(row["date"])
        fore = float(row["forward"])
        back = float(row["backward"])
        events.append(FactorEvent(symbol, effective, fore, back))
        last_event = effective
    return tuple(events), SourceHash(str(path), hashlib.sha256(raw_file).hexdigest())


def _load_stock_basic_anchor(path: Path, symbol: str, first_bar: date,
                             read: Callable[[Path], bytes]) -> tuple[ListingAnchorEvidence, SourceHash]:
    from core.calculation import ListingAnchorEvidence, SourceHash
    """Use a normalized security fact as listing-anchor evidence."""
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    code = normalize_security_id(symbol)
    rows, source_hash = validate_facts(
        payload, kind="security", code=code, start=None, end=None, year=None)
    row = rows[0]
    if row["type"] != "1":
        raise ValueError("security fact type or status is invalid")
    ipo_date = date.fromisoformat(row["listing_date"])
    out_date = None if row["delisting_date"] is None else date.fromisoformat(row["delisting_date"])
    if out_date is not None and out_date < first_bar:
        raise ValueError("security delisting predates first bar")
    if ipo_date != first_bar:
        raise ValueError(f"security listing {ipo_date} does not equal first raw bar {first_bar}")
    return (ListingAnchorEvidence(symbol, ipo_date, str(path), source_hash),
            SourceHash(str(path), hashlib.sha256(file_bytes).hexdigest()))


def _load_factor_history(cache_dir: Path, symbol: str, start: date, end: date,
                         changes: tuple[dict, ...], read: Callable[[Path], bytes],
                         template: str = 'adjust_{symbol}_{start}_{end}.json'):
    from core.calculation import FactorEvent
    """Use explicit alias intervals, checking unchanged basis at each seam.

    A rename is not a distribution. A numerical discontinuity requires further
    source evidence, never automatic rescaling or a stock-specific override.
    Prefix fore factors retain their provider basis; only back factors are
    used by this signal adapter.
    """
    incoming = {}
    for change in changes:
        if change['new_code'] in incoming:
            raise ValueError('ambiguous code-change history')
        incoming[change['new_code']] = change
    def load(code, visited):
        if code in visited:
            raise ValueError('cyclic code-change history')
        path = cache_dir / template.format(symbol=normalize_security_id(code), start=start, end=end)
        events, source = _load_factor_cache(path, code, start, end, read)
        if code not in incoming:
            return events, (source,)
        change = incoming[code]
        if not change['factor_basis_continuity_verified']:
            raise ValueError('unverified code-change history')
        before, hashes = load(change['old_code'], visited | {code})
        cut = change['effective_date']
        prefix = tuple(event for event in before if event.effective_date < cut)
        suffix = tuple(event for event in events if event.effective_date >= cut)
        if not prefix or not suffix or suffix[0].effective_date != cut:
            raise ValueError('missing factor seam evidence')
        if not math.isclose(prefix[-1].back_factor, suffix[0].back_factor,
                            rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError('contradictory rename factor basis')
        renamed = tuple(FactorEvent(code, event.effective_date, event.fore_factor,
                                    event.back_factor) for event in prefix)
        return (renamed + suffix,
                hashes + (source,))
    return load(symbol, set())


def _load_code_changes(path: Path, read: Callable[[Path], bytes]) -> tuple[tuple[dict, ...], SourceHash]:
    from core.calculation import SourceHash
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("events"), list):
        raise ValueError("unsupported code-change evidence schema")
    changes = []
    outgoing = {}
    incoming = set()
    for item in payload["events"]:
        if not isinstance(item, dict):
            raise ValueError("malformed code-change event")
        if not re.fullmatch(r"\d{6}", str(item.get("old_code", ""))) or not re.fullmatch(
            r"\d{6}", str(item.get("new_code", ""))
        ):
            raise ValueError("invalid old or new code in code-change evidence")
        try:
            effective_date = date.fromisoformat(item["effective_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid code-change effective date") from exc
        verified = item.get("factor_basis_continuity_verified")
        if not isinstance(verified, bool):
            raise ValueError("code-change factor-basis verdict is missing")
        old_code, new_code = item["old_code"], item["new_code"]
        if old_code == new_code or old_code in outgoing or new_code in incoming:
            raise ValueError("ambiguous code-change graph")
        change = {"old_code": old_code, "new_code": new_code,
                  "effective_date": effective_date,
                  "factor_basis_continuity_verified": verified}
        outgoing[old_code] = change
        incoming.add(new_code)
        changes.append(change)
    for start in outgoing:
        code, previous_date, visited = start, None, set()
        while code in outgoing:
            if code in visited:
                raise ValueError("cyclic code-change graph")
            visited.add(code)
            change = outgoing[code]
            if previous_date is not None and change["effective_date"] <= previous_date:
                raise ValueError("reverse code-change dates")
            previous_date = change["effective_date"]
            code = change["new_code"]
    return tuple(changes), SourceHash(str(path), hashlib.sha256(file_bytes).hexdigest())


def _float_or_nan(value: object, field: str, code: str, day: str,
                  *, positive: bool) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool):
        raise ValueError(f"{code} {day}: invalid {field}: {value!r}")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0) or (not positive and number < 0):
        raise ValueError(f"{code} {day}: invalid {field}: {value!r}")
    return number


def _parquet_paths_for_dates(paths: tuple[Path, ...], start: date, end: date) -> tuple[Path, ...]:
    import pyarrow.parquet as pq

    selected=[]
    for path in paths:
        parquet=pq.ParquetFile(path)
        column=parquet.schema_arrow.get_field_index('date')
        if column < 0:
            raise ValueError(f'parquet date field missing: {path}')
        intersects=False
        for index in range(parquet.metadata.num_row_groups):
            stats=parquet.metadata.row_group(index).column(column).statistics
            if stats is None or not stats.has_min_max:
                intersects=True
                break
            low,high=stats.min,stats.max
            low=low.date() if isinstance(low,datetime) else low
            high=high.date() if isinstance(high,datetime) else high
            if not isinstance(low,date):
                low=date.fromisoformat(str(low))
            if not isinstance(high,date):
                high=date.fromisoformat(str(high))
            if low<=end and high>=start:
                intersects=True
                break
        if intersects:
            selected.append(path)
    return tuple(selected)


class _BarEvidenceError(ValueError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


def _traded_bar(status: object, prices: tuple[float, float, float, float],
                volume: float, amount: float) -> bool:
    """One source-status/turnover predicate for all codes and dates.

    A suspended row may carry yesterday's positive close for valuation; it is
    not an observation for MA or returns. An unknown status with no turnover
    is not silently classified as either a trade or a suspension.
    """
    if status is not None:
        if isinstance(status, bool) or status not in (0, 0.0, 1, 1.0):
            raise _BarEvidenceError("invalid_trade_status", f"status={status!r}")
        if status == 0:
            if (math.isfinite(volume) and volume > 0) or (math.isfinite(amount) and amount > 0):
                raise _BarEvidenceError("contradictory_trade_state",
                                        f"status=0, volume={volume}, amount={amount}")
            return False
    if math.isfinite(volume) and volume > 0 and math.isfinite(amount) and amount > 0:
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise _BarEvidenceError("invalid_lake_price", f"traded OHLC={prices}")
        # RMB turnover / shares must lie inside the raw daily trading range.
        # A 1% tolerance absorbs source rounding, not a change of price basis.
        vwap = amount / volume
        if not prices[2] * 0.99 <= vwap <= prices[1] * 1.01:
            raise _BarEvidenceError("inconsistent_price_basis",
                                    f"VWAP={vwap}, low={prices[2]}, high={prices[1]}")
        return True
    if status == 1:
        raise _BarEvidenceError("contradictory_trade_state",
                                f"status=1, volume={volume}, amount={amount}")
    if ((not math.isfinite(volume) or volume == 0)
            and (not math.isfinite(amount) or amount == 0)):
        raise _BarEvidenceError("unknown_trade_state",
                                f"status missing, volume={volume}, amount={amount}, OHLC={prices}")
    raise _BarEvidenceError("contradictory_trade_state",
                            f"status missing, volume={volume}, amount={amount}")


def _load_adjusted_lake_panel(
    daily_dir: Path,
    cache_dir: Path,
    *,
    factor_start: date,
    factor_end: date,
    code_changes_path: Path,
    stock_basic_dir: Path | None = None,
    symbols: tuple[str, ...] | None = None,
    date_start: date | None = None,
    date_end: date | None = None,
    factor_template: str = 'adjust_{symbol}_{start}_{end}.json',
    stock_basic_template: str = 'stock_basic_{provider_code}.json',
    read: Callable[[Path], bytes],
    list_files: Callable[[Path, str], tuple[Path, ...]],
) -> AdjustedPricePanel:
    from core.calculation import (AdjustedPricePanel, AdjustmentCoverageError, CoverageIssue,
                                  SourceHash)
    """Load the *entire* supplied A-share lake with fail-closed factor coverage.

    A first bar preceding the first adjustment requires a verified listing date
    equal to that bar and no unresolved security code change. Listing identity
    and adjustment coverage come from standard fact records.
    """
    daily_dir, cache_dir = Path(daily_dir), Path(cache_dir)
    if not isinstance(factor_start, date) or not isinstance(factor_end, date) or factor_start > factor_end:
        raise ValueError("valid factor_start and factor_end dates are required")
    paths = sorted(list_files(daily_dir, "*.parquet"))
    if date_start is not None and date_end is not None:
        paths=list(_parquet_paths_for_dates(tuple(paths),date_start,date_end))
    if not paths:
        raise AdjustmentCoverageError((CoverageIssue("*", "missing_lake", str(daily_dir)),))
    connection = duckdb.connect()
    lake_hashes = tuple(SourceHash(str(path), _sha256_file(path, read)) for path in paths)
    try:
        changes, identity_hash = _load_code_changes(Path(code_changes_path), read)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AdjustmentCoverageError((CoverageIssue(
            "*", "invalid_code_change_evidence", str(exc)),)) from exc
    files = [str(path) for path in paths]
    # Resolve the storage key before selection and grouping. Price arithmetic
    # uses the A-share number; the dataset port restores the requested identity.
    source = ("(SELECT split_part(identity, '.', 2) AS code, * EXCLUDE(code, identity) "
              "FROM (SELECT *, CASE WHEN contains(code, '.') THEN lower(code) "
              "WHEN starts_with(code, '6') THEN 'sh.' || code ELSE 'sz.' || code END AS identity "
              "FROM read_parquet(?, union_by_name=true))")
    query_params = [files]
    if symbols is not None:
        source += " WHERE identity IN (SELECT * FROM UNNEST(?))"
        query_params.append([normalize_security_id(symbol) for symbol in symbols])
    source += ")"
    code_filter = ""
    date_filter = " AND " if code_filter else " WHERE "
    if date_start is not None:
        date_filter += "CAST(date AS DATE) >= ?"
        query_params.append(date_start)
    if date_end is not None:
        date_filter += (" AND " if date_start is not None else "") + "CAST(date AS DATE) <= ?"
        query_params.append(date_end)
    if date_start is None and date_end is None:
        date_filter = ""
    row_filter = code_filter + date_filter
    try:
        bounds = connection.execute(
            "SELECT code, min(date), max(date), count(*), count(DISTINCT date), "
            "arg_max(name, date) FROM " + source + row_filter + " GROUP BY code ORDER BY code",
            query_params,
        ).fetchall()
        calendar_strings = [row[0] for row in connection.execute(
            "SELECT DISTINCT date FROM " + source + row_filter + " ORDER BY date", query_params
        ).fetchall()]
        if not bounds or not calendar_strings:
            raise AdjustmentCoverageError((CoverageIssue("*", "empty_lake", str(daily_dir)),))
        issues: list[CoverageIssue] = []
        symbols: list[str] = []
        names: list[str] = []
        factor_events: list[FactorEvent] = []
        listing_anchors: list[ListingAnchorEvidence] = []
        cache_hashes: list[SourceHash] = []
        stock_basic_hashes: list[SourceHash] = []
        events_by_code: dict[str, tuple[FactorEvent, ...]] = {}
        listing_anchor_codes: set[str] = set()
        for code, first_string, last_string, count, distinct_count, name in bounds:
            code = str(code)
            symbols.append(code)
            names.append(str(name))
            if count != distinct_count:
                issues.append(CoverageIssue(code, "duplicate_lake_bar",
                                            f"{count - distinct_count} duplicate code-date rows"))
            try:
                market_code = normalize_security_id(code)
                first = date.fromisoformat(str(first_string))
                last = date.fromisoformat(str(last_string))
            except (TypeError, ValueError) as exc:
                issues.append(CoverageIssue(code, "invalid_lake_identity_or_date", str(exc)))
                continue
            if first < factor_start or last > factor_end:
                issues.append(CoverageIssue(code, "outside_factor_query_window",
                                            f"bars {first}..{last}, query {factor_start}..{factor_end}"))
                continue
            unresolved = [change for change in changes
                          if not change["factor_basis_continuity_verified"]
                          and ((change["new_code"] == code and first < change["effective_date"])
                               or (change["old_code"] == code and last >= change["effective_date"]))]
            if unresolved:
                change = unresolved[0]
                issues.append(CoverageIssue(
                    code, "unverified_code_change",
                    f"{change['old_code']}->{change['new_code']} on {change['effective_date']}"))
                continue
            cache_path = cache_dir / factor_template.format(
                symbol=normalize_security_id(code), start=factor_start, end=factor_end)
            try:
                events, history_hashes = _load_factor_history(
                    cache_dir, code, factor_start, factor_end, changes, read,
                    factor_template)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                reason = "missing_factor_cache" if isinstance(exc, FileNotFoundError) else "invalid_factor_history"
                issues.append(CoverageIssue(code, reason, f"{cache_path}: {exc}"))
                continue
            cache_hashes.extend(history_hashes)
            if not events:
                issues.append(CoverageIssue(code, "missing_initial_anchor",
                                            f"first raw bar {first}, no factor events"))
                continue
            if events[0].effective_date > first:
                if stock_basic_dir is None:
                    issues.append(CoverageIssue(
                        code, "missing_initial_anchor",
                        "per-code stock-basic and code-change evidence are required"))
                    continue
                stock_basic_path = Path(stock_basic_dir) / stock_basic_template.format(
                    symbol=normalize_security_id(code), provider_code=market_code)
                try:
                    anchor, source_hash = _load_stock_basic_anchor(stock_basic_path, code, first, read)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    reason = ("missing_stock_basic_snapshot" if isinstance(exc, FileNotFoundError)
                              else "missing_initial_anchor" if "does not equal first raw bar" in str(exc)
                              else "invalid_stock_basic_snapshot")
                    issues.append(CoverageIssue(code, reason, f"{stock_basic_path}: {exc}"))
                    continue
                listing_anchors.append(anchor)
                stock_basic_hashes.append(source_hash)
                listing_anchor_codes.add(code)
            events_by_code[code] = events
            factor_events.extend(events)
        calendar = None
        try:
            calendar = tuple(date.fromisoformat(str(value)) for value in calendar_strings)
        except (TypeError, ValueError) as exc:
            issues.append(CoverageIssue('*', 'invalid_lake_date', str(exc)))
        symbol_tuple = tuple(symbols)
        arrays = None
        traded_bar = None
        # Failed factor/identity evidence still needs independent bar auditing,
        # but cannot produce a panel. Do not allocate its dense price matrices.
        if not issues:
            shape = (len(symbol_tuple), len(calendar))
            arrays = {field: np.full(shape, np.nan, dtype=np.float64)
                      for field in ("open", "high", "low", "close", "volume", "amount")}
            traded_bar = np.zeros(shape, dtype=bool)
        bar_issue_counts: dict[tuple[str, str], int] = {}
        bar_issue_examples: dict[tuple[str, str], str] = {}
        checked_bars = 0
        code_index = {code: row for row, code in enumerate(symbol_tuple)}
        date_index = {str(day): column for column, day in enumerate(calendar_strings)}
        reader = connection.execute(
            "SELECT code, date, open, high, low, close, volume, amount, tradestatus "
            "FROM " + source + row_filter + " ORDER BY code, date", query_params,
        ).to_arrow_reader(batch_size=100_000)
        for batch in reader:
            columns = batch.to_pydict()
            for values in zip(*(columns[field] for field in
                                ("code", "date", "open", "high", "low", "close", "volume", "amount", "tradestatus")),
                              strict=True):
                code, day, raw_open, raw_high, raw_low, raw_close, volume, amount, status = values
                checked_bars += 1
                code, day = str(code), str(day)
                try:
                    numbers = {field: _float_or_nan(value, field, code, day, positive=False)
                               for field, value in (
                        ("open", raw_open), ("high", raw_high),
                        ("low", raw_low), ("close", raw_close),
                        ("volume", volume), ("amount", amount),
                    )}
                    prices = tuple(numbers[field] for field in
                                   ("open", "high", "low", "close"))
                    traded = _traded_bar(status, prices, numbers['volume'], numbers['amount'])
                    if arrays is not None:
                        row, column = code_index[code], date_index[day]
                        for field, number in numbers.items(): arrays[field][row, column] = number
                        traded_bar[row, column] = traded
                except _BarEvidenceError as exc:
                    reason, detail = exc.reason, str(exc)
                except (TypeError, ValueError, OverflowError) as exc:
                    reason, detail = "invalid_lake_price", str(exc)
                else:
                    continue
                key = (code, reason)
                bar_issue_counts[key] = bar_issue_counts.get(key, 0) + 1
                bar_issue_examples.setdefault(key, f"{day}: {detail}")
        if bar_issue_counts:
            issues.extend(
                CoverageIssue(code, reason,
                              f"{count} row(s); example {bar_issue_examples[(code, reason)]}")
                for (code, reason), count in sorted(bar_issue_counts.items())
            )
        for source in lake_hashes:
            if _sha256_file(Path(source.path), read) != source.sha256:
                issues.append(CoverageIssue("*", "lake_changed_during_load", source.path))
        if issues:
            raise AdjustmentCoverageError(tuple(issues), checked_bars=checked_bars)

        factor = np.full(shape, np.nan, dtype=np.float64)
        ordinal_calendar = np.asarray([day.toordinal() for day in calendar], dtype=np.int64)
        for row, code in enumerate(symbol_tuple):
            events = events_by_code[code]
            dates = np.asarray([event.effective_date.toordinal() for event in events], dtype=np.int64)
            values = np.asarray([event.back_factor for event in events], dtype=np.float64)
            positions = np.searchsorted(dates, ordinal_calendar, side="right") - 1
            valid = (positions >= 0) & np.isfinite(arrays["close"][row])
            factor[row, valid] = values[positions[valid]]
            if code in listing_anchor_codes:
                before_first_event = (positions < 0) & np.isfinite(arrays["close"][row])
                factor[row, before_first_event] = 1.0
        priced = np.isfinite(arrays["close"])
        unanchored = priced & ~np.isfinite(factor)
        if unanchored.any():
            row, column = np.argwhere(unanchored)[0]
            raise AdjustmentCoverageError((CoverageIssue(
                symbol_tuple[row], "missing_bar_factor", str(calendar[column])),))
        return AdjustedPricePanel(
            calendar=calendar, symbols=symbol_tuple, names=tuple(names),
            raw_open=arrays["open"], raw_high=arrays["high"],
            raw_low=arrays["low"], raw_close=arrays["close"],
            volume=arrays["volume"], amount=arrays["amount"],
            back_factor=factor, traded_bar=traded_bar,
            signal_close=np.where(traded_bar, arrays["close"] * factor, np.nan),
            signal_high=np.where(traded_bar, arrays["high"] * factor, np.nan),
            factor_events=tuple(factor_events),
            listing_anchors=tuple(listing_anchors),
            source_hashes=lake_hashes + tuple(cache_hashes) + tuple(stock_basic_hashes)
            + (identity_hash,),
        )
    finally:
        connection.close()
def _load_dataset(data, query):
    from core.calculation import (normalize_dividend_records, reconcile_corporate_actions,
                                  TaxLedger, FeePolicy, TaxPolicy)
    from strategy import canonical_hash
    provided={'open','high','low','close','volume','amount','signal_close'}
    if query.frequency!='daily' or query.price_basis!='adjusted':
        raise ValueError('historical adapter supports daily adjusted signal data only')
    dataset_id=query.dataset
    folder=data.path('sources',dataset_id,'manifest.json').parent
    def safe_path(root, name):
        path=(root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('historical evidence path escapes snapshot')
        return path
    def shared_ref(name):
        parts=Path(name).parts
        if not parts or parts[0]!='source':
            return None
        if len(parts)<2 or any(part in ('','.','..') for part in parts):
            raise ValueError('invalid shared source reference')
        identity=parts[1]
        relative=Path(*parts[2:]).as_posix() if len(parts)>2 else ''
        return identity,relative
    def source_ref(name):
        shared=shared_ref(name)
        if shared is not None:
            identity,relative=shared
            if not relative:
                return data.path('sources',identity,'_').parent, identity, relative
            return data.path('sources',identity,relative), identity, relative
        path=safe_path(folder,name)
        return path, dataset_id, path.relative_to(folder).as_posix()
    def related_name(base, child):
        if shared_ref(child) is not None:
            return child
        _, identity, relative = source_ref(base)
        parent = Path(relative).parent
        joined = (parent / child).as_posix()
        if identity == dataset_id:
            return joined
        return f"source/{identity}/{joined}"
    def read(name):
        _, identity, relative=source_ref(name)
        return data.read('sources',identity,relative)
    def refs_under(directory):
        shared=shared_ref(directory)
        if shared is not None:
            identity,relative=shared
            prefix=relative.rstrip('/')+'/' if relative else ''
            return tuple(ref for ref in data.list('sources',identity)
                         if ref.name.startswith(prefix))
        prefix=directory.rstrip('/')+'/'
        return tuple(ref for ref in data.list('sources',dataset_id)
                     if ref.name.startswith(prefix))
    def ref_name(directory, name):
        shared=shared_ref(directory)
        if shared is not None:
            identity,_=shared
            return f"source/{identity}/{Path(name).as_posix()}"
        return name
    manifest=data.manifest(dataset_id)
    overlap=set(manifest.get('fields',{}))&provided
    if overlap: raise ValueError('reserved price fields cannot be redefined: '+', '.join(sorted(overlap)))
    scope=manifest['scope']
    if not isinstance(scope['universe'],str) or not scope['universe'].strip(): raise ValueError('explicit dataset universe scope required')
    if query.universe!='dataset' and query.universe!=scope['universe']:
        raise ValueError(f'universe_scope_mismatch: 策略要求 {query.universe}；数据仅为 {scope["universe"]}，不能替代全市场。')
    extra={name:manifest['fields'][name] for name in set(query.fields)-provided
           if name in manifest.get('fields',{})}
    missing=set(query.fields)-provided-set(extra)
    if missing: raise ValueError('unsupported_fields: '+', '.join(sorted(missing)))
    for name,spec in extra.items():
        if (set(spec)!={'column','unit'} or any(not isinstance(spec[key],str) or not spec[key].strip() for key in spec)):
            raise ValueError('declared numeric field column and unit required: '+name)

    refs=manifest['files']
    if not isinstance(refs,list) or not refs: raise ValueError('historical evidence files are required')
    bound_names=set()
    hashes={}
    names_by_path={}
    market_files={}
    account_files={}
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get('file'), str):
            raise ValueError('historical evidence file reference is malformed')
        if ref['file'] in bound_names:
            raise ValueError('duplicate historical evidence file')
        bound_names.add(ref['file'])
    def evidence(name):
        path, _, _ = source_ref(name)
        if str(path) not in hashes:
            hashes[str(path)]=sha256(read(name)).hexdigest()
            names_by_path[str(path)]=name
        account_files[str(path)]=hashes[str(path)]
        return path
    def market_evidence(name):
        path=evidence(name)
        market_files[str(path)]=hashes[str(path)]
        return path
    inputs=manifest.get('inputs')
    if not isinstance(inputs,dict) or not isinstance(inputs.get('calendar'),str):
        raise ValueError('calendar input must name a calendar file')
    calendar_name=inputs['calendar']
    market_evidence(calendar_name)
    calendar_payload=json.loads(read(calendar_name))
    calendar, coverage = _calendar_facts(calendar_payload)
    calendar_start=coverage[0][0]
    calendar_end=coverage[-1][1]
    bars_dir=inputs['bars']
    adjustments=inputs['adjustments']
    factor_dir=adjustments['template'].rsplit('/', 1)[0]
    securities=inputs['securities']
    stock_dir=securities['template'].rsplit('/', 1)[0]
    changes_name=inputs['code_changes']
    policy_name=inputs['account_policy']
    factor_start=date.fromisoformat(adjustments['start'])
    factor_end=date.fromisoformat(adjustments['end'])
    declared=tuple(scope.get('symbols', ()))
    declared_ids=tuple(normalize_security_id(symbol) for symbol in declared)
    if len(set(declared_ids)) != len(declared_ids):
        raise ValueError('declared security scope contains duplicate identities')
    requested_ids=None if query.symbols is None else tuple(
        normalize_security_id(symbol) for symbol in query.symbols)
    if requested_ids is not None and len(set(requested_ids)) != len(requested_ids):
        raise ValueError('requested symbols contain duplicate identities')
    if requested_ids is not None and declared_ids and not set(requested_ids) <= set(declared_ids):
        raise ValueError('requested symbols are outside supplied dataset')
    if query.universe!='dataset' and scope['universe']!=query.universe:
        raise ValueError('universe_scope_mismatch: dataset scope does not satisfy the requested universe')
    if query.start < calendar[0] or query.end > calendar[-2]:
        raise ValueError('requested interval is outside historical snapshot')
    if query.universe!='dataset' and requested_ids is not None and set(requested_ids)!=set(declared_ids):
        raise ValueError('universe_scope_mismatch: requested subset does not satisfy complete universe')
    from bisect import bisect_left, bisect_right
    start_index=bisect_left(calendar,query.start)
    end_index=bisect_right(calendar,query.end)-1
    if start_index >= len(calendar)-1 or end_index < start_index:
        raise ValueError('requested interval contains no trading sessions')
    warmup=max(query.history,LIQUIDITY_WINDOW)
    if start_index < warmup:
        raise ValueError(f'insufficient pre-start history: requires {warmup} sessions (strategy and execution)')
    read_start=calendar[start_index-warmup]
    actual_start=calendar[start_index]
    actual_end=calendar[end_index]
    plugins=manifest.get('signal_plugins',{})
    if not isinstance(plugins,dict):
        raise ValueError('signal_plugins must be a mapping')
    scaler=plugins.get('scaler')
    if scaler is not None and isinstance(scaler,dict) and isinstance(scaler.get('fit_start'),str):
        fit_start=date.fromisoformat(scaler['fit_start'])
        fit_index=bisect_left(calendar,fit_start)
        if fit_index >= len(calendar)-1:
            raise ValueError('signal scaler fit window is not fully covered')
        read_start=calendar[min(start_index-warmup,fit_index)]
    read_start_index=calendar.index(read_start)
    read_end=actual_end
    sentinel=calendar[end_index+1]
    if not _calendar_covers(coverage, read_start, sentinel):
        raise ValueError('calendar coverage does not cover selected warm-up, interval, and sentinel')
    external_roots={}
    for directory in (bars_dir, factor_dir, stock_dir):
        shared=shared_ref(directory)
        if shared is not None:
            identity,_=shared
            external_roots[identity]=data.path('sources',identity,'_').parent
    def read_path(path):
        if path.is_relative_to(folder):
            identity=dataset_id
            relative=path.relative_to(folder).as_posix()
            name=relative
        else:
            source_root=data.path('sources','_','_').parents[1]
            if not path.is_relative_to(source_root):
                raise ValueError('unbound historical evidence path')
            relative_path=path.relative_to(source_root)
            if len(relative_path.parts)<2:
                raise ValueError('unbound historical evidence path')
            identity=relative_path.parts[0]
            relative=Path(*relative_path.parts[1:]).as_posix()
            name=f'source/{identity}/{relative}'
        payload=data.read('sources',identity,relative)
        digest=sha256(payload).hexdigest()
        hashes[str(path)]=digest
        names_by_path[str(path)]=name
        market_files[str(path)]=digest
        return payload
    def list_files(directory, pattern):
        shared=shared_ref(directory)
        if shared is not None:
            identity,relative=shared
            prefix=relative.rstrip('/')+'/' if relative else ''
            return tuple(data.path('sources',identity,ref.name)
                         for ref in data.list('sources',identity)
                         if ref.name.startswith(prefix)
                         and ref.name.endswith(pattern.removeprefix('*')))
        for identity, root in external_roots.items():
            if Path(directory).is_relative_to(root):
                prefix=Path(directory).relative_to(root).as_posix().rstrip('/')+'/'
                return tuple(data.path('sources',identity,ref.name)
                             for ref in data.list('sources',identity)
                             if ref.name.startswith(prefix)
                             and ref.name.endswith(pattern.removeprefix('*')))
        prefix=Path(directory).relative_to(folder).as_posix().rstrip('/')+'/'
        return tuple(data.path('sources',dataset_id,ref.name)
                     for ref in data.list('sources',dataset_id)
                     if ref.name.startswith(prefix)
                     and ref.name.endswith(pattern.removeprefix('*')))
    selected_symbols=(declared if requested_ids is None and declared else
                      None if requested_ids is None else
                      tuple(symbol.split('.')[-1] for symbol in requested_ids))
    panel=_load_adjusted_lake_panel(
        source_ref(bars_dir)[0],source_ref(factor_dir)[0],
        factor_start=factor_start,factor_end=factor_end,
        code_changes_path=market_evidence(changes_name),stock_basic_dir=source_ref(stock_dir)[0],
        symbols=selected_symbols,date_start=read_start,date_end=read_end,
        factor_template=Path(adjustments['template']).name,
        stock_basic_template=Path(securities['template']).name,
        read=read_path,
        list_files=list_files)
    scope = dict(scope)
    scope['calendar_start'] = calendar_start.isoformat()
    scope['calendar_end'] = calendar[-2].isoformat()
    if requested_ids is None and declared_ids and set(normalize_security_id(symbol) for symbol in panel.symbols) != set(declared_ids):
        raise ValueError('declared security scope differs from supplied historical lake')
    if tuple(calendar[read_start_index:end_index+1]) != panel.calendar:
        raise ValueError('price dates do not cover selected warm-up and query interval')
    start,end=query.start,query.end
    as_of=query.as_of
    if as_of is not None and as_of < actual_end:
        raise ValueError('data as_of precedes query end')
    if as_of is not None and factor_end != as_of:
        raise ValueError('point-in-time factor snapshot must end exactly at query cutoff')
    calendar = tuple(calendar[read_start_index:end_index+2])
    # Each requested code/year cache is explicit evidence, including successful empty results.
    coverage_template=inputs['distributions']['template']
    source_events={}; query_keys=set(); observations=[]; facts={}
    selected_codes=tuple(panel.symbols)
    for code in selected_codes:
      for year in range(read_start.year, actual_end.year + 1):
        name=coverage_template.format(symbol=normalize_security_id(code),year=year)
        path=market_evidence(name); payload=json.loads(read(name))
        records, _ = validate_facts(payload, kind='dividend',
            code=normalize_security_id(code), start=None, end=None, year=str(year))
        query_keys.add((code,year))
        for row,record in enumerate(records):
            try:
                event=normalize_dividend_records([{**record,'code':normalize_security_id(code)}])[0]
            except ValueError as error:
                raise ValueError(f'{name}, record {row}: {error}') from error
            event=replace(event,code=code)
            if event.registration_date is not None and read_start<=event.registration_date<=actual_end:
                if not event.executable: raise ValueError('corporate action settlement evidence incomplete')
                if not record.get('announce_date') or not record.get('registration_date'):
                    raise ValueError('cash distribution identity dates are required')
                distribution_id=f"{code}:{record['announce_date']}:{record['registration_date']}"
                source=canonical_hash({'file':name,'row':row,'hash':hashes[str(path)]})
                observations.append((source,distribution_id,event))
                source_events[(name,row)]=(event,hashes[str(path)])
                cash_only=(event.stock_dividend_per_share == 0 and event.reserve_to_stock_per_share == 0)
                if cash_only:
                    facts[(code,distribution_id)]=((None,Decimal('1'),Decimal('0')),
                        [(hashes[str(path)],hashes[str(path)])])
                else:
                    for item in inputs.get('action_facts') or ():
                        if item.get('source_file') == name and item.get('source_row') == row:
                            credit_date=item.get('credit_date')
                            credit_source=item.get('credit_source_file')
                            if not isinstance(credit_date,str) or not credit_date.strip():
                                raise ValueError('noncash corporate action requires a valid credit date')
                            try:
                                parsed_credit_date=date.fromisoformat(credit_date)
                            except ValueError as error:
                                raise ValueError('noncash corporate action credit date is invalid') from error
                            if parsed_credit_date.isoformat()!=credit_date:
                                raise ValueError('noncash corporate action credit date is invalid')
                            if not isinstance(credit_source,str) or not credit_source.strip():
                                raise ValueError('noncash corporate action requires a credit source file')
                            terms=(item.get('credit_date'),Decimal(item['stock_nominal_per_share']),
                                   Decimal(item['reserve_taxable_per_share']))
                            facts[(code,distribution_id)]=(terms,[(hashes[str(path)],hashes[str(evidence(item['tax_source_file']))])])
                            market_evidence(credit_source)
                            break
                    if (code,distribution_id) not in facts:
                        raise ValueError('noncash corporate action requires sourced credit and tax facts')
    policy_file=evidence(policy_name)
    policy=json.loads(read(policy_name))
    if policy['schema']!=1 or policy['investor']!='domestic_individual_unrestricted_a_share':
        raise ValueError('unsupported investor account policy')
    for source in policy['sources']:
        source_name=related_name(policy_name,source['file'])
        path=evidence(source_name)
        if hashes[str(path)]!=source['sha256']: raise ValueError('policy source hash mismatch')
    policy_hash=hashes[str(policy_file)]
    fees=FeePolicy.from_mapping(policy['fees'],policy_hash)
    taxes=TaxPolicy.from_mapping(policy['tax'],policy_hash)
    if start<taxes.effective: raise ValueError('requested start precedes supported tax effective date')
    matched={}
    day_inputs={day:[] for day in calendar[:-1]}
    for action in reconcile_corporate_actions(observations,{code:code for code in selected_codes}):
        terms,sources=facts[(action.security_id,action.distribution_id)]
        credit,nominal,reserve=terms
        output_code=(query.symbols[requested_ids.index(normalize_security_id(action.security_id))]
                     if requested_ids is not None else action.event.code)
        event=replace(action.event,code=output_code)
        source_hash=canonical_hash({'observations':[source for source,_ in action.observations],'sources':sources})
        tax_hash=canonical_hash({'sources':sources,'policy':policy_hash})
        TaxLedger(taxes).register(action.distribution_id,event,nominal,reserve,tax_hash,
            credit_date=None if credit is None else date.fromisoformat(credit))
        day_inputs[event.registration_date].append(RegistrationAction(action.distribution_id,event,source_hash,
            None if credit is None else date.fromisoformat(credit),nominal,reserve,tax_hash))
    field_data={}
    if extra:
        import pyarrow as pa
        import pyarrow.parquet as pq
        refs=tuple(ref for ref in refs_under(bars_dir) if ref.name.endswith('.parquet'))
        files=tuple(source_ref(ref_name(bars_dir,ref.name))[0] for ref in refs)
        selected_paths=set(_parquet_paths_for_dates(files,panel.calendar[0],panel.calendar[-1]))
        columns=['code','date',*sorted({spec['column'] for spec in extra.values()})]
        paths=[]
        for ref in refs:
            file_name=ref_name(bars_dir, ref.name)
            path,_,_=source_ref(file_name)
            if path not in selected_paths:
                continue
            market_evidence(file_name)
            parquet=pq.ParquetFile(path)
            schema=parquet.schema_arrow
            for name,spec in extra.items():
                if spec['column'] not in schema.names:
                    raise ValueError('declared field column missing: '+name)
                kind=schema.field(spec['column']).type
                if not (pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)):
                    raise ValueError('declared field column must be numeric: '+name)
            paths.append(str(path))
        code_filter=''
        params=[paths]
        if panel.symbols:
            code_filter=' WHERE code IN (SELECT * FROM UNNEST(?))'
            params.append(list(panel.symbols))
        date_filter=' AND ' if code_filter else ' WHERE '
        date_filter+='CAST(date AS DATE) BETWEEN ? AND ?'
        params.extend((panel.calendar[0],panel.calendar[-1]))
        connection=duckdb.connect()
        try:
            table=connection.execute(
                'SELECT '+', '.join(columns)+' FROM read_parquet(?)'+code_filter+date_filter,
                params).to_arrow_table()
        finally:
            connection.close()
        codes={normalize_security_id(symbol):i for i,symbol in enumerate(panel.symbols)}
        dates={day:j for j,day in enumerate(panel.calendar)}
        field_data={name:np.full(panel.raw_close.shape,np.nan) for name in extra}
        seen=set()
        for row in table.to_pylist():
            code=normalize_security_id(str(row['code'])); raw_day=row['date']
            day=raw_day if isinstance(raw_day,date) else date.fromisoformat(raw_day)
            if code not in codes or day not in dates:
                raise ValueError('numeric field row outside selected price scope')
            identity=(code,day)
            if identity in seen: raise ValueError('duplicate numeric field security/date')
            seen.add(identity)
            i,j=codes[code],dates[day]
            for name,spec in extra.items():
                value=row[spec['column']]
                if value is None or not np.isfinite(float(value)): raise ValueError(f'numeric field missing/nonfinite: {name}, {code}, {day}')
                field_data[name][i,j]=float(value)
        for name,values in field_data.items():
            selected=list(range(len(panel.symbols)))
            if np.any(np.isfinite(panel.raw_close[selected])&~np.isfinite(values[selected])):
                raise ValueError('numeric field lacks aligned observed price rows: '+name)
    if query.symbols is not None:
        rows={normalize_security_id(symbol):index for index,symbol in enumerate(panel.symbols)}
        order=tuple(rows[identity] for identity in requested_ids)
        output_symbols=tuple(query.symbols)
        output_by_id=dict(zip(requested_ids,output_symbols))
        reorder=lambda values: values[list(order),:]
        panel=replace(panel,symbols=output_symbols,
            names=tuple(panel.names[index] for index in order),
            raw_open=reorder(panel.raw_open),raw_high=reorder(panel.raw_high),
            raw_low=reorder(panel.raw_low),raw_close=reorder(panel.raw_close),
            volume=reorder(panel.volume),amount=reorder(panel.amount),
            back_factor=reorder(panel.back_factor),traded_bar=reorder(panel.traded_bar),
            signal_close=reorder(panel.signal_close),signal_high=reorder(panel.signal_high),
            factor_events=tuple(replace(event,code=output_by_id[normalize_security_id(event.code)])
                                for event in panel.factor_events),
            listing_anchors=tuple(replace(anchor,code=output_by_id[normalize_security_id(anchor.code)])
                                  for anchor in panel.listing_anchors))
        field_data={name:reorder(values) for name,values in field_data.items()}
    if as_of is not None:
        availability=manifest.get('source_availability')
        if not isinstance(availability,list) or not availability:
            raise ValueError('point-in-time query requires unified source availability evidence')
        by_file={}
        for item in availability:
            if (not isinstance(item,dict) or set(item)!={'file','sha256','available_at'}
                    or item['file'] in by_file):
                raise ValueError('invalid unified source availability evidence')
            by_file[item['file']]=item
        for absolute,digest in hashes.items():
            name=names_by_path[absolute]
            item=by_file.get(name)
            if item is None or item['sha256']!=digest:
                raise ValueError(f'point-in-time source availability is not bound: {name}')
            try:
                available_day=date.fromisoformat(item['available_at'])
            except (TypeError,ValueError) as error:
                raise ValueError(f'invalid source availability date: {name}') from error
            if available_day>as_of:
                raise ValueError(f'source evidence became available after query cutoff: {name}')
        if not any(factor_dir in name for name in names_by_path.values()):
            raise ValueError('point-in-time query requires factor availability evidence')
    data_hash=canonical_hash({'files':market_files,'symbols':panel.symbols,'universe':scope['universe'],
        'inputs':inputs,'price_basis':'backward-factor-signal-index','fields':manifest.get('fields',{})})
    return (panel, calendar, {day: tuple(actions) for day, actions in day_inputs.items()},
            hashes, data_hash, {'policy': policy, 'source_hash': policy_hash},
            canonical_hash({'files': account_files, 'facts': [
                (identity, tuple(None if value is None else str(value) for value in terms), sources)
                for identity, (terms, sources) in sorted(facts.items())]}),
            field_data, scope, plugins)




def _prepare_raw_dataset(data, query):
    import pyarrow as pa
    import pyarrow.parquet as pq
    compare_only = query.purpose == "compare"
    required = set(query.fields) if compare_only else {"open", "high", "low", "close", "volume", "amount"}
    if query.frequency != "daily" or query.price_basis != "raw":
        raise ValueError("raw market queries require daily frequency")
    if not set(query.fields) <= required:
        raise ValueError("raw market fields must be OHLCVA")
    manifest = data.manifest(query.dataset)
    calendar_name = manifest.get("calendar")
    if not isinstance(calendar_name, str):
        raise ValueError("daily dataset calendar is required")
    calendar_payload = json.loads(data.read("sources", query.dataset, calendar_name))
    sessions, coverage = _calendar_facts(calendar_payload)
    if query.start not in sessions or query.end not in sessions:
        raise ValueError("requested interval is outside historical snapshot")
    start_index = sessions.index(query.start)
    end_index = sessions.index(query.end)
    if end_index + 1 >= len(sessions):
        raise ValueError("daily dataset must provide a following session")
    first_index = max(0, start_index - query.history + 1)
    run_days = sessions[first_index:end_index + 1]
    read_days = run_days + (sessions[end_index + 1],)
    if not _calendar_covers(coverage, run_days[0], read_days[-1]):
        raise ValueError("calendar coverage does not cover raw query warm-up, interval, and sentinel")
    needed_years = {str(day.year) for day in read_days}
    start_value = run_days[0]
    end_value = run_days[-1]
    scope = dict(manifest["scope"])
    declared = tuple(scope.get("symbols", ()))
    if not declared:
        raise ValueError("daily dataset symbols are required")
    declared_ids = {normalize_security_id(symbol) for symbol in declared}
    if query.symbols is not None:
        requested_ids = tuple(normalize_security_id(symbol) for symbol in query.symbols)
        if not set(requested_ids) <= declared_ids:
            raise ValueError("requested symbols are outside supplied dataset")
        symbols = tuple(query.symbols)
    else:
        symbols = declared
        requested_ids = tuple(normalize_security_id(symbol) for symbol in symbols)
    # Read both the requested spelling and the declared spelling.  Providers
    # may write 600036 or sh.600036 for the same canonical identity.
    read_symbols = tuple(sorted(set(declared) | set(requested_ids)))
    rows = {}
    source_hashes = {}
    for ref in manifest["files"]:
        name = ref["file"]
        parts = Path(name).parts
        if not name.endswith(".parquet") or "daily" not in parts:
            continue
        daily_index = parts.index("daily")
        identity = parts[1] if parts[0] == "source" else query.dataset
        relative = Path(*parts[daily_index:]).as_posix()
        stem = Path(parts[-1]).stem
        if stem.isdigit() and len(stem) == 4 and stem not in needed_years:
            continue
        payload = data.read("sources", identity, relative)
        source_hashes[name] = sha256(payload).hexdigest()
        parquet = pq.ParquetFile(pa.BufferReader(payload))
        names = set(parquet.schema_arrow.names)
        if not {"code", "date"} | set(query.fields) <= names:
            raise ValueError("daily Parquet fields are incomplete")
        date_type = parquet.schema_arrow.field("date").type
        date_value = start_value if pa.types.is_date(date_type) else start_value.isoformat()
        end_value_for_filter = end_value if pa.types.is_date(date_type) else end_value.isoformat()
        # ``tradestatus`` is an internal integrity input.  It is read only
        # when the published table provides it; it is never added to the
        # caller's requested fields or returned in the query result.
        columns = ["code", "date", *required, *query.fields]
        if "tradestatus" in names:
            columns.append("tradestatus")
        columns = list(dict.fromkeys(columns))
        table = pq.read_table(
            pa.BufferReader(payload), columns=columns,
            filters=[("code", "in", list(read_symbols)),
                     ("date", ">=", date_value),
                     ("date", "<=", end_value_for_filter)],
        )
        for row in table.to_pylist():
            key = (normalize_security_id(str(row.get("code"))), str(row.get("date")))
            if not key[0] or key[0] == "None":
                raise ValueError("daily row code is required")
            day = date.fromisoformat(key[1])
            value = dict(row)
            if key in rows:
                previous = rows[key]
                for field, item in value.items():
                    if field == 'code':
                        continue
                    if previous.get(field) is not None and item is not None and previous[field] != item:
                        raise ValueError(f'conflicting daily input: {key} {field}')
                    if previous.get(field) is None:
                        previous[field] = item
            else:
                rows[key] = value
    if not source_hashes:
        raise ValueError("daily dataset Parquet files are required")
    history = {symbol: deque(maxlen=query.history) for symbol in symbols}
    prepared = []
    for column, day in enumerate(run_days):
        fields, bars = {}, {}
        opening, closing = {}, {}
        for symbol, identity in zip(symbols, requested_ids):
            row = rows.get((identity, day.isoformat()), {})
            values = {field: row.get(field) for field in required}
            try:
                numbers = {
                    field: _float_or_nan(value, field, str(row.get("code")),
                                         str(day), positive=False)
                    for field, value in values.items()
                }
                if compare_only:
                    traded = False
                else:
                    prices = tuple(numbers[field] for field in
                                   ("open", "high", "low", "close"))
                    traded = _traded_bar(row.get("tradestatus"), prices,
                                         numbers["volume"], numbers["amount"])
            except _BarEvidenceError as exc:
                raise ValueError(f"{exc.reason}: {exc}") from exc
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid daily bar: {exc}") from exc
            selected = {field: values[field] for field in query.fields}
            at = datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai"))
            history_bar = HistoryBar(at, selected)
            history[symbol].append(history_bar)
            fields[symbol] = selected
            if traded and all(values[field] is not None for field in EXECUTION_FIELDS):
                bars[symbol] = Bar(symbol, day, *(float(values[field]) for field in EXECUTION_FIELDS))
            if not compare_only and all(math.isfinite(numbers[field]) for field in ("open", "close")):
                opening[symbol] = Decimal(str(values["open"]))
                closing[symbol] = Decimal(str(values["close"]))
        next_session = sessions[first_index + column + 1]
        frame = MarketFrame(
            datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai")),
            "MARKET", fields, {"next_session": next_session.isoformat()},
            {symbol: tuple(history[symbol]) for symbol in symbols},
        )
        prepared.append(HistoryDay(
            day, frame, bars, {}, opening, closing, {}, {},
            sessions[first_index + column - 1] if column else date.min,
            (), {},
        ))
    scope["symbols"] = symbols
    scope["calendar_start"] = sessions[0].isoformat()
    scope["calendar_end"] = sessions[-2].isoformat()
    return Dataset(
        query.dataset, tuple(prepared), {"kind": "raw"},
        {"source_hashes": source_hashes, "price_basis": "raw",
         "data_hash": sha256(json.dumps(source_hashes, sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()},
        scope, tuple(run_days) + (sessions[end_index + 1],), query.fields, query.history,
    )


def _prepare_dataset(data, query):
    if query.price_basis == "raw":
        return _prepare_raw_dataset(data, query)
    from core.calculation import Calculation, FeePolicy, TaxPolicy
    panel, calendar, account_inputs, hashes, data_hash, policy, account_hash, field_data, scope, plugins = _load_dataset(data, query)
    end_index = max(index for index, day in enumerate(calendar[:-1]) if day <= query.end)
    run_calendar = calendar[:end_index + 2]
    amount_liquidity = Calculation.own_day_rolling_mean(panel.amount, LIQUIDITY_WINDOW, panel.traded_bar)
    volume_liquidity = Calculation.own_day_rolling_mean(panel.volume, LIQUIDITY_WINDOW, panel.traded_bar)
    own_history = {symbol: deque(maxlen=query.history) for symbol in panel.symbols}
    session_history = {symbol: deque(maxlen=query.history) for symbol in panel.symbols}
    last_marks, last_liquidity, last_volume = {}, {}, {}
    prepared = []
    for column, day in enumerate(run_calendar[:-1]):
        at = datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai"))
        fields, bars, opening, closing = {}, {}, {}, {}
        opening_adv, closing_adv, opening_volume20, closing_volume20 = {}, {}, {}, {}
        for row, symbol in enumerate(panel.symbols):
            values = {name: float(array[row, column]) if np.isfinite(array[row, column]) else None
                      for name, array in (
                          ("open", panel.raw_open), ("high", panel.raw_high),
                          ("low", panel.raw_low), ("close", panel.raw_close),
                          ("volume", panel.volume), ("amount", panel.amount),
                          ("signal_close", panel.signal_close))}
            values.update({name: float(array[row, column]) if np.isfinite(array[row, column]) else None
                           for name, array in field_data.items()})
            selected = {name: values[name] for name in query.fields}
            history_bar = HistoryBar(at, selected)
            session_history[symbol].append(history_bar)
            if panel.traded_bar[row, column]:
                own_history[symbol].append(history_bar)
            fields[symbol] = selected
            if any(values[name] is None for name in EXECUTION_FIELDS):
                continue
            bars[symbol] = Bar(symbol, day, *(values[name] for name in EXECUTION_FIELDS))
            opening[symbol] = Decimal(str(values["open"]))
            closing[symbol] = Decimal(str(values["close"]))
            if symbol in last_liquidity:
                opening_adv[symbol] = last_liquidity[symbol]
            if symbol in last_volume:
                opening_volume20[symbol] = last_volume[symbol]
            if np.isfinite(amount_liquidity[row, column]):
                closing_adv[symbol] = Decimal(str(amount_liquidity[row, column]))
            if np.isfinite(volume_liquidity[row, column]):
                closing_volume20[symbol] = Decimal(str(volume_liquidity[row, column]))
        frame = MarketFrame(
            at, "CN_EQUITY", fields,
            {"next_session": run_calendar[column + 1].isoformat()},
            {symbol: tuple(session_history[symbol]) for symbol in panel.symbols},
        )
        prepared.append(HistoryDay(
            day, frame, bars, dict(last_marks), opening, closing,
            opening_adv, closing_adv, calendar[column - 1] if column else date.min,
            account_inputs[day], opening_volume20,
        ))
        last_marks.update({symbol: float(value) for symbol, value in closing.items()})
        last_liquidity.update(closing_adv)
        last_volume.update(closing_volume20)
    plugin_provenance = None
    if plugins:
        prepared, plugin_provenance = _apply_signal_plugins(prepared, plugins, query.start, query.end)
    provenance = {
        "source_hashes": hashes, "data_hash": data_hash,
        "account_hash": account_hash, "state_start": calendar[0].isoformat(),
        "as_of": None if query.as_of is None else query.as_of.isoformat(),
        "query_semantics": "point_in_time" if query.as_of is not None else "snapshot_replay",
    }
    if plugin_provenance is not None:
        provenance["signal_plugins"] = plugin_provenance
    return Dataset(
        query.dataset, tuple(prepared),
        {"fees": FeePolicy.from_mapping(policy["policy"]["fees"], policy["source_hash"]),
         "tax_policy": TaxPolicy.from_mapping(policy["policy"]["tax"], policy["source_hash"])},
        provenance, {**scope, "symbols": tuple(panel.symbols)},
        tuple(run_calendar), query.fields, query.history,
    )


def _apply_signal_plugins(records, plugins, start, end):
    from dataclasses import replace
    from protocal import HistoryBar, MarketFrame
    from importlib import import_module
    from plugins.data import Adapter
    from strategy import canonical_hash

    def names(value, label):
        value = tuple(value)
        if (not value or len(set(value)) != len(value)
                or any(not isinstance(field, str) or not field for field in value)):
            raise ValueError(f'{label} must be unique non-empty names')
        return value

    def projection(record, selected):
        fields = {}
        for symbol, values in record.frame.fields.items():
            if any(field not in values for field in selected):
                raise ValueError('signal plugin field is missing from strategy input')
            fields[symbol] = {field: values[field] for field in selected}
        history = {}
        for symbol, rows in record.frame.history.items():
            history[symbol] = tuple(
                HistoryBar(row.at, {field: row.fields[field] for field in selected})
                for row in rows
            )
        return replace(record.frame, fields=fields, history=history)

    def checked_output(original, output, selected):
        output = tuple(output)
        if len(output) != len(original):
            raise ValueError('signal plugin output length changed')
        for before, after in zip(original, output):
            if not isinstance(after, MarketFrame) or after.at != before.at:
                raise ValueError('signal plugin output timestamp changed')
            if tuple(after.fields) != tuple(before.fields):
                raise ValueError('signal plugin output symbols changed')
            for symbol in before.fields:
                if tuple(after.fields[symbol]) != selected:
                    raise ValueError('signal plugin output fields changed')
            if tuple(after.history) != tuple(before.history):
                raise ValueError('signal plugin output history symbols changed')
            for symbol in before.history:
                before_times = tuple(row.at for row in before.history[symbol])
                after_times = tuple(row.at for row in after.history[symbol])
                if after_times != before_times:
                    raise ValueError('signal plugin output history timestamps changed')
                current = next((row for row in after.history[symbol] if row.at == after.at), None)
                if current is not None and dict(current.fields) != dict(after.fields[symbol]):
                    raise ValueError('signal plugin output current frame/history differs')
        Adapter.validate(output, fields=selected)
        return output

    def merge(original, transformed, selected):
        output = []
        for record, frame in zip(original, transformed):
            fields = {symbol: dict(values) for symbol, values in record.frame.fields.items()}
            for symbol in fields:
                fields[symbol].update(frame.fields[symbol])
            history = {}
            for symbol, rows in record.frame.history.items():
                if tuple(row.at for row in rows) != tuple(row.at for row in frame.history[symbol]):
                    raise ValueError('signal plugin merge would discard history rows')
                changed = {row.at: row for row in frame.history[symbol]}
                history[symbol] = tuple(replace(row, fields={**row.fields, **changed[row.at].fields})
                                        for row in rows)
            output.append(replace(record, frame=replace(record.frame, fields=fields, history=history)))
        return tuple(output)

    if not isinstance(plugins, dict) or set(plugins) - {'adapter', 'scaler'}:
        raise ValueError('unsupported signal plugin declaration')
    provenance = {}
    adapter_spec = plugins.get('adapter')
    if adapter_spec is not None:
        if (not isinstance(adapter_spec, dict)
                or set(adapter_spec) - {'module', 'method', 'fields', 'params'}
                or adapter_spec.get('method') not in {'validate', 'align'}
                or not adapter_spec.get('fields')):
            raise ValueError('signal adapter requires module, method, fields, and params')
        module_name = adapter_spec.get('module')
        if not isinstance(module_name, str) or ':' not in module_name:
            raise ValueError('signal adapter module must be explicit')
        module, name = adapter_spec['module'].split(':', 1)
        adapter = getattr(import_module(module), name)
        method = getattr(adapter, adapter_spec['method'], None)
        if not callable(method):
            raise ValueError('signal adapter method is not callable')
        selected_fields = names(adapter_spec['fields'], 'signal adapter fields')
        params = dict(adapter_spec.get('params', {}))
        signal_frames = tuple(projection(record, selected_fields) for record in records)
        if adapter_spec['method'] == 'align':
            transformed = method(signal_frames, tuple(frame.at for frame in signal_frames), **params)
        else:
            transformed = method(signal_frames, **params)
        transformed = checked_output(signal_frames, transformed, selected_fields)
        merged = merge(records, transformed, selected_fields)
        provenance['adapter'] = {
            'module': module_name, 'method': adapter_spec['method'],
            'fields': selected_fields,
        }
        records = merged

    scaler_spec = plugins.get('scaler')
    if scaler_spec is not None:
        if (not isinstance(scaler_spec, dict)
                or set(scaler_spec) - {'module', 'fields', 'fit_start', 'fit_end', 'params'}
                or not scaler_spec.get('module') or not scaler_spec.get('fields')
                or not isinstance(scaler_spec.get('fit_start'), str)
                or not isinstance(scaler_spec.get('fit_end'), str)):
            raise ValueError('signal scaler requires module, fields, fit_start, fit_end, and params')
        try:
            fit_start = date.fromisoformat(scaler_spec['fit_start'])
            fit_end = date.fromisoformat(scaler_spec['fit_end'])
        except ValueError as error:
            raise ValueError('signal scaler fit dates are invalid') from error
        if fit_end >= start:
            raise ValueError('signal scaler fit end before backtest start is required')
        available = tuple(record.trade_date for record in records)
        if fit_start not in available or fit_end not in available:
            raise ValueError('signal scaler fit window is not fully covered')
        module_name = scaler_spec['module']
        if not isinstance(module_name, str) or ':' not in module_name:
            raise ValueError('signal scaler module must be explicit')
        module, name = module_name.split(':', 1)
        scaler_type = getattr(import_module(module), name)
        selected_fields = names(scaler_spec['fields'], 'signal scaler fields')
        training = []
        for record in records:
            if fit_start <= record.trade_date <= fit_end:
                for values in record.frame.fields.values():
                    training.append([values[field] for field in selected_fields])
        if not training:
            raise ValueError('signal scaler fit window is not fully covered')
        scaler = scaler_type(**dict(scaler_spec.get('params', {})))
        try:
            scaler.fit(training)
            statistics = scaler.statistics()
            transformed = []
            for record in records:
                frames = projection(record, selected_fields)
                values = []
                locations = []
                for symbol, fields in frames.fields.items():
                    values.append([fields[field] for field in selected_fields])
                    locations.append((symbol, fields))
                for symbol, rows in frames.history.items():
                    for row in rows:
                        values.append([row.fields[field] for field in selected_fields])
                        locations.append((symbol, row.fields))
                output = iter(scaler.transform(values))
                fields = {}
                for symbol, original in locations[:len(frames.fields)]:
                    result = next(output)
                    fields[symbol] = {field: float(result[index]) for index, field in enumerate(selected_fields)}
                history = {}
                offset = len(frames.fields)
                for symbol, rows in frames.history.items():
                    history[symbol] = []
                    for row in rows:
                        result = next(output)
                        history[symbol].append(HistoryBar(
                            row.at, {field: float(result[index]) for index, field in enumerate(selected_fields)}))
                    history[symbol] = tuple(history[symbol])
                transformed.append(replace(frames, fields=fields, history=history))
            transformed = Adapter.validate(tuple(transformed), fields=selected_fields)
        except Exception as error:
            raise ValueError(f'signal scaler transform: {error}') from error
        records = merge(records, transformed, selected_fields)
        serial_statistics = {
            key: value.tolist() if hasattr(value, 'tolist') else value
            for key, value in statistics.items()
        }
        provenance['scaler'] = {
            'module': module_name, 'fields': selected_fields,
            'fit_start': fit_start.isoformat(), 'fit_end': fit_end.isoformat(),
            'statistics': serial_statistics,
            'statistics_hash': canonical_hash(serial_statistics),
        }
    return tuple(records), provenance


def build_data(root: Path) -> DataBus:
    """Explicit composition for offline and backend data access."""
    from storage import DataStore
    return DataBus(DataStore(root))
@dataclass(frozen=True)
class OperationSpec:
    name: str
    mode: Literal["read", "write"]
    input_schema: str
    output_schema: str
    kind: str
    port: PortKind


@dataclass(frozen=True)
class PortResult:
    """模块端口返回的协议中立结果。"""

    kind: str
    type: str
    schema: str
    data: JsonValue


@dataclass(frozen=True)
class SearchSpec:
    updater: object
    max_trials: int
    metric: str
    direction: str
    declaration: dict[str, object]


class ModulePort(TypingProtocol):
    def handle(self, operation: str, data: JsonValue) -> PortResult | JsonValue: ...


@dataclass(frozen=True)
class Modules:
    """协议需要的五个模块端口。端口实现可以由运行时装配。"""

    backtests: ModulePort
    research: ModulePort
    sources: ModulePort
    lake: ModulePort
    commands: ModulePort

    def port(self, name: PortKind) -> ModulePort:
        return getattr(self, name)


_OPERATIONS: tuple[OperationSpec, ...] = (
    OperationSpec("workbench.strategies", "read", "strategies", "strategies", "collection", "backtests"),
    OperationSpec("workbench.preflight", "read", "preflight", "preflight", "document", "backtests"),
    OperationSpec("workbench.backtest", "write", "backtest", "backtest-result", "resource", "backtests"),
    OperationSpec("cli.backtest", "write", "cli-backtest", "cli-backtest", "operation", "commands"),
    OperationSpec("collection.describe", "read", "collection-describe", "collection-describe", "document", "sources"),
    OperationSpec("collection.inspect", "read", "collection-inspect", "collection-inspect", "document", "sources"),
    OperationSpec("collection.run", "write", "collection-run", "collection-run", "operation", "sources"),
    OperationSpec("cli.ingest", "write", "cli-ingest", "cli-ingest", "operation", "commands"),
    OperationSpec("workbench.runs", "read", "runs", "runs", "collection", "research"),
    OperationSpec("workbench.run", "read", "run", "run", "resource", "research"),
    OperationSpec("workbench.benchmarks", "read", "benchmarks", "benchmarks", "collection", "research"),
    OperationSpec("workbench.benchmark", "read", "benchmark", "benchmark", "resource", "research"),
    OperationSpec("lake.researchInputs", "read", "research-inputs", "research-inputs", "document", "lake"),
    OperationSpec("workbench.sources", "read", "sources", "sources", "collection", "sources"),
    OperationSpec("workbench.sync", "write", "sync", "sync", "operation", "sources"),
    OperationSpec("workbench.schedule", "write", "schedule", "schedule", "operation", "sources"),
    OperationSpec("workbench.job", "read", "job", "job", "resource", "sources"),
    OperationSpec("workbench.snapshot", "read", "snapshot", "snapshot", "resource", "sources"),
    OperationSpec("workbench.evidence", "read", "evidence", "evidence", "collection", "sources"),
    OperationSpec("workbench.failedEvidence", "read", "failed-evidence", "failed-evidence", "collection", "sources"),
    OperationSpec("workbench.reviews", "read", "reviews", "reviews", "collection", "sources"),
    OperationSpec("workbench.review", "read", "review", "review", "resource", "sources"),
    OperationSpec("lake.status", "read", "lake-status", "lake-status", "resource", "lake"),
    OperationSpec("lake.query", "read", "lake-query", "lake-query", "collection", "lake"),
    OperationSpec("lake.kline", "read", "kline", "kline", "series", "lake"),
    OperationSpec("lake.startFetch", "write", "start-fetch", "fetch-job", "operation", "lake"),
    OperationSpec("lake.fetchJob", "read", "fetch-job", "fetch-job", "resource", "lake"),
    OperationSpec("lake.stopFetch", "write", "stop-fetch", "stop-fetch", "operation", "lake"),
    OperationSpec("lake.continuousFetch", "read", "continuous-fetch", "continuous-fetch", "resource", "lake"),
    OperationSpec("lake.setContinuousFetch", "write", "set-continuous-fetch", "continuous-fetch", "operation", "lake"),
    OperationSpec("lake.startMerge", "write", "start-merge", "merge-job", "operation", "lake"),
    OperationSpec("lake.mergeJob", "read", "merge-job", "merge-job", "resource", "lake"),
    OperationSpec("lake.doctor", "read", "doctor", "doctor", "document", "lake"),
    OperationSpec("lake.doctorFix", "write", "doctor-fix", "doctor-fix", "operation", "lake"),
    OperationSpec("lake.doctorJob", "read", "doctor-job", "doctor-job", "resource", "lake"),
    OperationSpec("cli.ingest", "write", "cli-ingest", "cli-ingest", "operation", "commands"),
)
_DIRECTORY = {item.name: item for item in _OPERATIONS}


def export_operations() -> dict[str, JsonObject]:
    """返回前端和文档可消费的唯一操作目录。"""

    return {
        item.name: {
            "mode": item.mode,
            "input_schema": f"urn:lianghua:ui:input:{item.name}:v1",
            "output_schema": f"urn:lianghua:ui:output:{item.name}:v1",
            "kind": item.kind,
            "port": item.port,
        }
        for item in _OPERATIONS
    }


class Benchmarks:
    def __init__(self, store, specs):
        from math import isfinite
        self.store = store
        self.specs = {item["id"]: item for item in specs}
        if len(self.specs) != len(specs):
            raise ValueError("duplicate benchmark identity")

    def list(self):
        return list(self.specs.values())

    def series(self, identity, start, end, *, dates=None):
        from math import isfinite
        if identity not in self.specs:
            raise KeyError(identity)
        if start > end:
            raise ValueError("起始日期不能晚于截止日期")
        spec = self.specs[identity]
        dataset_id = spec.get("dataset_id")
        if isinstance(dataset_id, str) and dataset_id:
            requested_code = normalize_security_id(spec["code"])
            first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
            result = self.store.query(DataQuery(
                dataset_id, first, last, ("close",), 1, "raw",
                symbols=(requested_code,), universe="dataset", purpose="compare"))
            points = []
            for record in result.records:
                values = record.frame.fields.get(requested_code)
                if not isinstance(values, Mapping) or values.get("close") is None:
                    continue
                value = float(values["close"])
                if not isfinite(value) or value <= 0:
                    raise ValueError("基准收盘价必须为正数")
                points.append({"at": record.trade_date.isoformat(), "close": value})
            if not points:
                raise ValueError("基准标准行情在请求区间没有数据")
            if dates is not None and tuple(item["at"] for item in points) != tuple(dates):
                raise ValueError("基准日期不全或顺序不一致")
            return {**spec, "snapshot_id": result.provenance.get("data_hash"),
                    "collected_at": None, "start": start, "end": end,
                    "points": points}
        raise ValueError('基准必须登记标准dataset_id')

class Research:
    def __init__(self, data):
        self.data = data

    def _ids(self):
        names = {}
        for ref in self.data.list("runs"):
            names.setdefault(ref.identity, set()).add(ref.name)
        return tuple(sorted(identity for identity, files in names.items()
                           if {"manifest.json", "metrics.json", "result.json", "trace.json"} <= files))

    def _read(self, identity):
        from hashlib import sha256
        manifest_bytes = self.data.read("runs", identity, "manifest.json")
        metrics_bytes = self.data.read("runs", identity, "metrics.json")
        result_bytes = self.data.read("runs", identity, "result.json")
        manifest = json.loads(manifest_bytes)
        metrics = json.loads(metrics_bytes)
        result = json.loads(result_bytes)
        return {
            "id": identity, "run_id": manifest["run_id"],
            "strategy_id": manifest["strategy_id"], "status": manifest["status"],
            "qualification": "unverified", "limitations": manifest.get("limitations", []),
            "metrics": metrics, "manifest_file": f"data/result/{identity}/manifest.json",
            "manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "metrics_sha256": sha256(metrics_bytes).hexdigest(),
            "source_hashes": manifest.get("source_hashes", {}),
            "config_hash": manifest.get("config_hash"),
            "note": manifest.get("note", "运行结果未经历史资格认定。"),
            "parameters": manifest.get("parameters", {}),
            "effective_config": manifest.get("effective_config", {}),
            "code_hash": manifest.get("code_hash"), "data_hash": manifest.get("data_hash"),
            "execution_hash": manifest.get("execution_hash"), "result": result,
        }

    def list(self):
        return [self._read(identity) for identity in self._ids()]

    def detail(self, identity, offset=0, limit=50):
        if identity not in self._ids():
            raise KeyError(identity)
        value = self._read(identity)
        manifest = json.loads(self.data.read("runs", identity, "manifest.json"))
        refs = {(item.identity, item.name): item for item in self.data.list("runs")}
        result = value.pop("result")
        all_trades, all_equity = result.get("trades", []), result.get("equity", [])
        trades = all_trades[offset:offset + limit]
        stride = max(1, (len(all_equity) + 799) // 800)
        equity = all_equity[::stride]
        if all_equity and equity[-1] != all_equity[-1]:
            equity.append(all_equity[-1])
        value["trades_total"] = len(all_trades)
        value["equity_sampling"] = {"total": len(all_equity), "stride": stride}
        evidence = []
        for name in ("manifest.json", "metrics.json", "result.json", "trace.json"):
            ref = refs.get((identity, name))
            if ref is not None:
                evidence.append({"file": ref.file, "sha256": ref.sha256})
        return {**value, "manifest": manifest, "trades": trades,
                "trades_total": value["trades_total"], "equity": equity,
                "evidence": evidence, "offset": offset, "limit": limit}


def build_protocol(modules: Modules) -> Protocol:
    """用显式模块端口组装唯一协议入口。"""

    return Protocol(modules)


def save_backtest(data: DataPort, request: BacktestRequest, value: Mapping[str, Any]) -> None:
    metrics = value["metrics"]
    effective_config = value["effective_config"]
    manifest = {
        "schema": 1, "run_id": request.run_id, "strategy_id": request.strategy_id,
        "status": "succeeded", "dataset_id": request.dataset_id,
        "parameters": dict(request.parameters),
        "effective_config": effective_config,
        "config_hash": value.get("config_hash"),
        "source_hashes": value.get("provenance", {}).get("source_hashes", {}),
        "data_hash": value.get("provenance", {}).get("data_hash"),
        "execution_hash": value.get("provenance", {}).get("execution_hash"),
        "limitations": ["运行结果未经历史资格认定。"],
        "note": "运行结果未经历史资格认定。",
    }
    for name, item in (("metrics.json", metrics), ("result.json", value),
                       ("trace.json", value.get("trace", {})), ("manifest.json", manifest)):
        content = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        data.write("runs", request.run_id, name, content)


def save_failed_backtest(data: DataPort, run_id: str, error: Exception,
                         partial: object | None) -> str:
    value = {"run_id": run_id, "status": "failed", "code": getattr(error, "code", "run_failed"),
             "detail": str(error), "partial": _json_value(partial) if partial is not None else None}
    name = "failed.json"
    data.write("runs", run_id, name, json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    return f"data/result/{run_id}/{name}"


class CollectionPort:
    def __init__(self, collect):
        self.collect = collect
        self.data = collect.data

    def handle(self, operation, value):
        if operation == 'collection.describe':
            return json.loads(self.data.read('sources', 'registry', 'sources.json'))
        if operation == 'collection.run':
            result = self.collect.run(value)
            if result['status'] != 'succeeded':
                raise ValueError(result.get('error') or 'collection failed')
            return result
        if operation == 'collection.inspect':
            source = self.collect.sources.get(value['source_id'])
            result = {}
            if 'codes' in value:
                result['last_date'] = self.last_date(source, value['codes'])
            if 'start' in value and 'end' in value:
                request = self.collect.methods.prepare_request(source, value)
                coverage = self.collect.methods.coverage(source, request)
                if coverage is None:
                    raise ValueError('coverage is not registered for this source')
                result['coverage'] = dict(coverage)
                if source.get('method') == 'daily':
                    result['coverage']['calendar_evidence_complete'] = self.calendar_coverage(source, value['start'], value['end'])
                    result['coverage']['complete'] = coverage['complete'] and result['coverage']['calendar_evidence_complete']
            return json.loads(json.dumps(result, allow_nan=False))
        # Existing source operations retain their registered structured contract.
        return self.collect.handle(operation, value)

    def last_date(self, source: Mapping, codes) -> str | None:
        """Read the watermark for these securities, never an unrelated index."""
        if source.get('method') == 'daily':
            import duckdb
            from copy import deepcopy
            descriptor = deepcopy(dict(source))
            descriptor['defaults'] = {k: v for k, v in source.get('defaults', {}).items() if k != 'codes'}
            with self.data.transaction('sources'):
                _, _, _, _, files, _, _ = self.collect.methods._daily_view_context(descriptor, source['dataset_id'])
                paths = [str(self.data.path('sources', identity, name)) for identity, name in files]
                aliases = sorted({v for code in codes for v in
                                  (code, normalize_security_id(code), normalize_security_id(code).split('.')[-1])})
                with duckdb.connect() as connection:
                    row = connection.execute(
                        'SELECT max(date) FROM read_parquet(?, union_by_name=true) WHERE code IN (SELECT unnest(?))',
                        [paths, aliases]).fetchone()
                return str(row[0]) if row and row[0] is not None else None
        dates = [row['date'] for row in self.record_rows(source)
                 if row.get('code') in codes and row.get('date')]
        return max(dates) if dates else None

    def calendar_coverage(self, source, start, end):
        """Return whether registered calendar facts cover the requested range."""
        dataset = source['dataset_id']
        manifest = self.data.manifest(dataset)
        name = manifest.get('inputs', {}).get('calendar')
        if not isinstance(name, str):
            return False
        parts = Path(name).parts
        payload = (self.data.read('sources', parts[1], Path(*parts[2:]).as_posix())
                   if parts[0] == 'source' else self.data.read('sources', dataset, name))
        _, coverage = _calendar_facts(json.loads(payload))
        start_date = _fact_date(start, 'calendar query start')
        end_date = _fact_date(end, 'calendar query end')
        if start_date > end_date:
            raise ValueError('calendar query range is reversed')
        return _calendar_covers(coverage, start_date, end_date)

    def record_rows(self, source):
        with self.data.transaction('sources'):
            offset = 0
            while True:
                view = self.collect.methods.records(source, offset, 1000)
                if view is None:
                    return
                yield from view['records']
                offset += len(view['records'])
                if not view['records'] or offset >= view['total']:
                    return



def build_backend(root: Path) -> Protocol:
    """Compose the project once from the protocol data registry."""

    from collect.core import Collect
    from collect.methods import Methods
    from collect.sources import Sources

    project = Path(root).resolve()
    data = build_data(project)
    sources = Sources(data)
    registry = sources._registry
    datasets = registry.get("datasets", [])
    benchmarks_config = registry.get("benchmarks", [])
    if (not isinstance(datasets, list) or not isinstance(benchmarks_config, list)
            or any(not isinstance(item, dict) or not item.get("id") for item in datasets)
            or any(not isinstance(item, dict) or not item.get("id") for item in benchmarks_config)):
        raise ValueError("data registry datasets and benchmarks must be named records")
    from plugins.data import Settlement
    settlement_type = Settlement
    methods = Methods(data)
    sources_port = Collect(sources, methods)
    benchmarks = Benchmarks(data, benchmarks_config)
    backtests = KernelBacktestPort(
        data, settlement_type, benchmarks,
        datasets=datasets, research=Research(data),
    )
    lake = sources_port
    commands = CommandPort(project, backtests, data)
    return Protocol(Modules(backtests, backtests, CollectionPort(sources_port), lake, commands))


class KernelBacktestPort:
    """把固定 Kernel 适配成统一协议中的一个端口。"""

    def __init__(self, data: DataPort, settlement_type, benchmarks=None, *, datasets=(), research=None) -> None:
        if not callable(settlement_type):
            raise TypeError("settlement plugin must be callable")
        from core.kernel import Kernel
        self._kernel = Kernel(data, _load_registered_strategy, _query, settlement_type)
        self._data = data
        self._benchmarks = benchmarks
        self._datasets = tuple(datasets)
        self._research = research if research is not None else Research(data)

    def handle(self, operation: str, data: JsonValue) -> PortResult | JsonValue:
        if operation == "workbench.strategies":
            from strategy import catalog
            items = catalog()
            requirements = {}
            for item in items:
                strategy = _load_registered_strategy(item["module"], item["defaults"])
                requirements[item["module"]] = _config_value(strategy.requirements())
            datasets = list(self._datasets)
            return {"items": items, "availability": {
                "ready": bool(datasets), "datasets": datasets,
                "requirements": requirements,
                "issues": [] if datasets else [{
                    "code": "historical_data_unavailable",
                    "message": "尚无可用历史数据集。",
                }],
            }}
        if operation == "workbench.preflight":
            request = parse_backtest_request(data)
            try:
                prepared = self._kernel.prepare(request)
            except (BacktestError, DataError, TypeError, ValueError) as error:
                return {"ready": False,
                        "effective_params": getattr(error, "effective_params", dict(request.parameters)),
                        "requirements": getattr(error, "requirements", {}),
                        "config_hash": None,
                        "effective_config": getattr(error, "effective_config", {}),
                        "issues": [{"code": getattr(error, "code", "invalid_input"),
                                    "message": str(error)}]}
            return {"ready": True, "effective_params": prepared.effective_config["params"],
                    "requirements": prepared.effective_config["requirements"],
                    "config_hash": prepared.config_hash,
                    "effective_config": prepared.effective_config, "issues": []}
        if operation == "workbench.runs":
            return {"items": self._research.list()}
        if operation == "workbench.run":
            if not isinstance(data, dict):
                raise TypeError("operation data must be an object")
            return self._research.detail(data["id"], data.get("offset", 0), data.get("limit", 50))
        if operation == "workbench.benchmarks":
            return self._benchmarks.list()
        if operation == "workbench.benchmark":
            if not isinstance(data, dict):
                raise TypeError("operation data must be an object")
            return self._benchmarks.series(data["id"], data["start"], data["end"])
        if operation != "workbench.backtest":
            raise BacktestError("unsupported_operation", operation)
        request = parse_backtest_request(data)
        search = _search(data, request)
        try:
            if search is None:
                result = self._kernel.run(request)
            else:
                result = self._kernel.search(
                    request, search.updater, max_trials=search.max_trials,
                    metric=search.metric, direction=search.direction,
                )
        except BacktestError as error:
            if error.code == "busy":
                raise
            if search is None:
                partial = getattr(error, "partial_result", None)
                error.evidence_ref = save_failed_backtest(self._data, request.run_id, error, partial)
            else:
                error.evidence_ref = self._save_search_failure(request, error)
            raise
        if search is None:
            value = _result_data(result, request, self._benchmarks)
            save_backtest(self._data, request, value)
        else:
            try:
                value = self._save_search(request, search, result)
            except BacktestError as error:
                if error.code == "busy":
                    raise
                if not getattr(error, "evidence_ref", None):
                    error.evidence_ref = self._save_search_failure(request, error)
                raise
        return PortResult(
            "resource", "lianghua:backtest-result",
            "urn:lianghua:ui:output:workbench.backtest:v1", value,
        )

    def _save_search_failure(self, request: BacktestRequest, error: BacktestError) -> str:
        completed = getattr(error, "completed_trials", ())
        for trial in completed:
            save_backtest(self._data, trial.request, _result_data(trial.response, trial.request, self._benchmarks))
        failed_request = getattr(error, "failed_request", request)
        partial = getattr(error, "partial_result", None)
        evidence = {
            "completed_trials": [
                _result_data(trial.response, trial.request, self._benchmarks) for trial in completed
            ],
            "failed_request": _json_value(failed_request),
            "partial": _json_value(partial) if partial is not None else None,
        }
        if isinstance(partial, Mapping):
            evidence.update(_json_value(partial))
        if hasattr(error, "failed_values"):
            evidence["failed_values"] = _json_value(error.failed_values)
        if hasattr(error, "trial_index"):
            evidence["trial_index"] = error.trial_index
        return save_failed_backtest(self._data, request.run_id, error, evidence)

    def _save_search(self, request: BacktestRequest, spec: SearchSpec, result) -> JsonObject:
        from strategy import canonical_hash

        trials = []
        for trial in result.trials:
            value = _result_data(trial.response, trial.request, self._benchmarks)
            value["score"] = trial.score
            trials.append(value)
            save_backtest(self._data, trial.request, value)
            value["trial_config_hash"] = value.pop("config_hash")
        best = result.trials[result.best_index]
        value = dict(_result_data(best.response, best.request, self._benchmarks))
        effective_config = dict(value["effective_config"])
        declaration = dict(spec.declaration)
        declaration["base_parameters"] = dict(request.parameters)
        effective_config["search"] = declaration
        summary_hash = canonical_hash(effective_config)
        provenance = dict(value.get("provenance", {}))
        provenance["effective_config"] = effective_config
        provenance["config_hash"] = summary_hash
        value.update({
            "id": request.run_id,
            "run_id": request.run_id,
            "parameters": dict(best.request.parameters),
            "effective_config": effective_config,
            "config_hash": summary_hash,
            "provenance": provenance,
            "search": {
                "metric": spec.metric,
                "direction": spec.direction,
                "best_run_id": best.request.run_id,
                "best_index": result.best_index,
                "trials": trials,
            },
        })
        summary_request = replace(request, parameters=dict(best.request.parameters))
        save_backtest(self._data, summary_request, value)
        return value


class CommandPort:
    """CLI transport operations; no command owns a business execution path."""

    def __init__(self, root: Path, backtests: ModulePort, data: DataPort) -> None:
        self._root = Path(root).resolve()
        self._backtests = backtests
        self._data = data

    def handle(self, operation: str, data: JsonValue) -> JsonValue:
        if not isinstance(data, dict):
            raise TypeError("operation data must be an object")
        if operation == "cli.backtest":
            config = _decode_backtest_config(Path(_text(data, "config")).read_bytes())
            if not isinstance(config, dict) or "request" in config:
                raise ValueError("backtest config must be the request object without request wrapper")
            if "search" in config:
                raise ValueError("cli.backtest compares fixed parameters and does not accept search")
            if {"start", "end", "validation", "out_of_sample"} & set(config):
                raise ValueError("cli.backtest requires intervals and rejects top-level range fields")
            intervals = config.get("intervals")
            if not isinstance(intervals, list) or not intervals:
                raise ValueError("intervals must be a non-empty list")

            batch_id = config.get("run_id", uuid4().hex)
            prepared = []
            common = {key: value for key, value in config.items() if key != "intervals"}
            self._check_run_identity(batch_id)
            for index, interval in enumerate(intervals, 1):
                if not isinstance(interval, dict) or set(interval) - {"name", "start", "end"}:
                    raise ValueError(f"interval {index} must contain only name, start and end")
                name = interval.get("name")
                if name is not None and (not isinstance(name, str) or not name.strip()):
                    raise ValueError(f"interval {index} name must be a non-empty string or null")
                run_id = f"{batch_id}-interval-{index:04d}"
                self._check_run_identity(run_id)
                request = {**common, "run_id": run_id,
                           "start": interval.get("start"), "end": interval.get("end")}
                try:
                    parse_backtest_request(request)
                except BacktestError as error:
                    raise BacktestError(
                        error.code,
                        f"interval {index} ({name!r}, {run_id}): {error}",
                    ) from error
                prepared.append((index, name, run_id, request))

            completed = []
            for index, name, run_id, request in prepared:
                try:
                    result = self._backtests.handle("workbench.backtest", request)
                except Exception as error:
                    if isinstance(error, BacktestError) and error.code == "busy":
                        raise BacktestError(
                            "busy",
                            f"interval {index} ({name!r}, {run_id}): {error}",
                        ) from error
                    failure = BacktestError(
                        "interval_failed",
                        f"interval {index} ({name!r}, {run_id}) failed: {error}",
                    )
                    partial = {"failed": {"index": index, "name": name, "run_id": run_id},
                               "completed": completed}
                    failure.evidence_ref = save_failed_backtest(
                        self._data, batch_id, failure, partial,
                    )
                    raise failure from error
                value = result.data if isinstance(result, PortResult) else result
                completed.append({"index": index, "name": name, "run_id": run_id,
                                  "result": value})
            value = {"run_id": batch_id, "intervals": completed}
            return PortResult(
                "resource", "lianghua:cli-backtest",
                "urn:lianghua:ui:output:cli.backtest:v1", value,
            )
        if operation == "cli.ingest":
            from data.ingest import ingest_exploratory

            identity = _identity(data, "output")
            result = ingest_exploratory(
                price_workbook=Path(_text(data, "price")),
                fundamental_workbook=Path(_text(data, "fundamentals")),
                valuation_workbook=Path(_text(data, "valuations")),
                output_dir=self._data.path("sources", identity, "manifest.json").parent,
                max_symbols=data.get("max_symbols"),
                only_price_parts=(
                    tuple(data["price_part"]) if data.get("price_part") is not None else None
                ),
            )
            return asdict(result)
        raise ValueError(f"unsupported command operation: {operation}")

    def _check_run_identity(self, value: JsonValue) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("run_id must be a data identity")
        self._data.path("runs", value, "manifest.json")
        if len(os.fsencode(value)) > os.pathconf(self._root, "PC_NAME_MAX"):
            raise ValueError("run_id exceeds the storage identity length")

class Protocol:
    """后端唯一入口；每个请求只经过一个已声明的模块端口。"""

    def __init__(self, modules: Modules) -> None:
        self._modules = modules

    async def send(self, request) -> list:
        ui = import_module("frontend.ui-protocol")
        if not isinstance(request, ui.UiRequest):
            return [self._problem(None, "invalid_request", "request must be UiRequest")]
        spec = _DIRECTORY.get(request.operation)
        if spec is None:
            return [self._problem(request.request_id, "unknown_operation", request.operation)]
        expected_schema = f"urn:lianghua:ui:input:{spec.name}:v1"
        if request.schema != expected_schema:
            return [self._problem(request.request_id, "invalid_schema", expected_schema)]
        try:
            value = self._modules.port(spec.port).handle(spec.name, request.data)
            result = value if isinstance(value, PortResult) else PortResult(
                spec.kind, f"lianghua:{spec.output_schema}",
                f"urn:lianghua:ui:output:{spec.name}:v1", value,
            )
            return [ui.UiMessage(
                message_id=uuid4().hex, correlation_id=request.request_id,
                kind=result.kind, type=result.type, schema=result.schema, data=result.data,
            )]
        except DataError as error:
            return [self._problem(request.request_id, error.code, str(error))]
        except BacktestError as error:
            return [self._problem(request.request_id, error.code, str(error), error.code == "busy",
                                  getattr(error, "evidence_ref", None))]
        except (TypeError, ValueError) as error:
            return [self._problem(request.request_id, "invalid_input", str(error))]
        except Exception as error:
            return [self._problem(request.request_id, "module_failed", str(error))]

    def start(self) -> None:
        for port in self._ports():
            start = getattr(port, "start", None)
            if callable(start):
                start()

    def close(self) -> None:
        for port in reversed(self._ports()):
            close = getattr(port, "close", None)
            if callable(close):
                close()

    def _ports(self) -> tuple[ModulePort, ...]:
        unique = []
        for port in (
            self._modules.backtests, self._modules.research, self._modules.sources,
            self._modules.lake, self._modules.commands,
        ):
            if all(port is not item for item in unique):
                unique.append(port)
        return tuple(unique)

    @staticmethod
    def _problem(correlation_id: str | None, code: str, detail: str,
                 retryable: bool = False, evidence: str | None = None):
        ui = import_module("frontend.ui-protocol")
        problem = ui.UiProblem(
            type="urn:lianghua:problem:protocol", code=code,
            title="协议请求未完成", detail=detail, retryable=retryable,
            extensions={} if evidence is None else {"x-evidence": {"href": evidence}},
        )
        return ui.UiMessage(
            message_id=uuid4().hex, correlation_id=correlation_id,
            kind="problem", type=problem.type, schema=PROBLEM_SCHEMA, data=problem.to_dict(),
        )


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
                   "execution", "search"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unsupported request fields: {', '.join(sorted(unknown))}")
        settlement_parameters = {}
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
    tax = action("tax", {"fifo"}, ())
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


def _search(data: JsonValue, request: BacktestRequest) -> SearchSpec | None:
    if not isinstance(data, dict) or "search" not in data:
        return None
    value = data["search"]
    if not isinstance(value, dict):
        raise BacktestError("invalid_search", "search must be an object")
    allowed = {"updater", "max_trials", "metric", "direction"}
    unknown = set(value) - allowed
    if unknown:
        raise BacktestError("invalid_search", f"unsupported search fields: {', '.join(sorted(unknown))}")
    required = {"updater", "max_trials", "metric", "direction"}
    if not required <= set(value):
        raise BacktestError("invalid_search", "search requires updater, max_trials, metric and direction")
    max_trials = value["max_trials"]
    if type(max_trials) is not int or max_trials < 1:
        raise BacktestError("invalid_search", "max_trials must be a positive integer")
    metric = value["metric"]
    if not isinstance(metric, str) or not metric.strip():
        raise BacktestError("invalid_search", "metric must be a non-empty string")
    direction = value["direction"]
    if not isinstance(direction, str) or direction not in {"max", "min"}:
        raise BacktestError("invalid_search", "direction must be max or min")
    updater = _updater(value["updater"])
    declaration = {
        "updater": value["updater"], "max_trials": max_trials,
        "metric": metric, "direction": direction,
    }
    return SearchSpec(updater, max_trials, metric, direction, declaration)


def _updater(data: JsonValue):
    if not isinstance(data, dict) or set(data) != {"module", "method", "params"}:
        raise BacktestError("invalid_search", "updater must contain module, method and params")
    name = data["module"]
    method = data["method"]
    params = data["params"]
    if not isinstance(name, str) or name.count(":") != 1:
        raise BacktestError("invalid_search", "updater module must use module:Class")
    module_name, class_name = name.split(":")
    if (module_name != "core.calculation" and not module_name.startswith("plugins.")):
        raise BacktestError("invalid_search", "updater module must be a local core or plugin module")
    if (not module_name or any(part.startswith("_") for part in module_name.split("."))
            or not class_name or class_name.startswith("_")
            or not isinstance(method, str) or not method or method.startswith("_")
            or not isinstance(params, dict)):
        raise BacktestError("invalid_search", "invalid updater declaration")
    root = Path(__file__).resolve().parent
    try:
        module = import_module(module_name)
    except (ImportError, AttributeError) as error:
        raise BacktestError("invalid_search", "updater module is not available locally") from error
    location = getattr(module, "__file__", None)
    if (location is None or not Path(location).resolve().is_file()
            or not Path(location).resolve().is_relative_to(root)):
        raise BacktestError("invalid_search", "updater module is not a local module")
    try:
        owner = getattr(module, class_name)
        if not isinstance(owner, type):
            raise TypeError("updater target must be a class")
        descriptor = getattr_static(owner, method)
        if not isinstance(descriptor, (staticmethod, classmethod)):
            raise TypeError("updater method must be staticmethod or classmethod")
        target = getattr(owner, method)
        if not callable(target):
            raise TypeError("updater method must be callable")
        signature(target).bind((), **params)
    except (AttributeError, TypeError, ValueError) as error:
        raise BacktestError("invalid_search", str(error)) from error
    return partial(target, **dict(params))


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


def _query(request: BacktestRequest, requirements) -> DataQuery:
    required = tuple(requirements.fields)
    fields = required if not request.fields else tuple(request.fields)
    if not set(required) <= set(fields):
        raise BacktestError("invalid_query", "custom fields omit strategy requirements")
    fields = required_fields(fields)
    history = requirements.history_bars if request.history == 0 else request.history
    if history < requirements.history_bars:
        raise BacktestError("invalid_query", "custom history is shorter than strategy requirements")
    return DataQuery(
        dataset=request.dataset_id, start=request.start.date(), end=request.end.date(),
        fields=fields, history=history, price_basis=request.price_basis,
        as_of=request.as_of, universe="dataset", frequency=requirements.frequency,
        market=request.market,
    )


def _load_registered_strategy(name, parameters):
    from strategy import load_strategy
    if "." not in name:
        name = "plugins.strategies." + name
    return load_strategy(name, parameters)


def _load_plugin(settings, key):
    spec = settings.get(key)
    if not isinstance(spec, dict) or set(spec) - {"module", "params"}:
        raise ValueError(f"explicit {key} plugin declaration is required")
    name = spec.get("module")
    if not isinstance(name, str) or ":" not in name or not isinstance(spec.get("params", {}), dict):
        raise ValueError(f"invalid {key} plugin declaration")
    module, symbol = name.split(":", 1)
    if not module or not symbol:
        raise ValueError(f"invalid {key} plugin declaration")
    plugin = getattr(import_module(module), symbol)
    params = dict(spec["params"])
    if "execution" in params:
        params["execution"] = validate_execution(params["execution"])
    parameters = signature(plugin).parameters
    accepted = {name for name, parameter in parameters.items()
                if name not in {"self", "initial_cash", "fees", "tax_policy"}
                and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)}
    unknown = set(params) - accepted
    if unknown:
        raise ValueError(f"invalid {key} parameters: {', '.join(sorted(unknown))}")
    if "limits" in params:
        params["limits"] = _limits(params["limits"])

    def configured(*args, **overrides):
        merged = merge(overrides)
        return plugin(*args, **merged)

    def merge(overrides):
        unknown_overrides = set(overrides) - accepted
        if unknown_overrides:
            raise TypeError(f"invalid {key} overrides: {', '.join(sorted(unknown_overrides))}")
        merged = dict(params)
        merged.update(overrides)
        if "limits" in merged and isinstance(merged["limits"], dict):
            merged["limits"] = _limits(merged["limits"])
        if "execution" in merged:
            merged["execution"] = validate_execution(merged["execution"])
        return merged

    validate_dataset = getattr(plugin, "validate_dataset", None)
    if callable(validate_dataset):
        def configured_validate_dataset(dataset, request, **overrides):
            settlement_parameters = dict(request.settlement_parameters)
            settlement_parameters.update(overrides)
            return validate_dataset(dataset, request, **merge(settlement_parameters))

        configured.validate_dataset = configured_validate_dataset

    def describe(overrides):
        return {"module": name, "params": _config_value(merge(overrides))}

    configured.describe = describe
    return configured


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


def _result_data(result: BacktestResponse, request: BacktestRequest | None = None, benchmarks=None) -> JsonObject:
    value = {
        "id": result.run_id, "run_id": result.run_id, "strategy_id": result.strategy_id, "dataset_id": result.dataset_id,
        "first_at": result.first_at.isoformat(), "last_at": result.last_at.isoformat(),
        "steps": result.steps, "fills": result.fills, "final_cash": str(result.final_cash),
        "final_equity": str(result.final_equity),
        "positions": {symbol: str(quantity) for symbol, quantity in result.positions.items()},
        "pending_orders": result.pending_orders,
        "metrics": dict(result.metrics),
    }
    benchmark = None
    if request is not None and request.benchmark_id is not None:
        if benchmarks is None:
            raise BacktestError("benchmark_unavailable", "benchmark service is not configured")
        if result.account_result is None:
            raise BacktestError("benchmark_unavailable", "benchmark requires account observations")
        actual = tuple(point.day for point in result.account_result.equity
                       if point.day >= result.first_at.date() and point.day <= result.last_at.date())
        if not actual:
            raise BacktestError("benchmark_unavailable", "account has no benchmark comparison dates")
        start, end = actual[0].isoformat(), actual[-1].isoformat()
        series = benchmarks.series(request.benchmark_id, start, end,
                                   dates=tuple(day.isoformat() for day in actual))
        if series.get("return_type") != "price":
            raise BacktestError("benchmark_invalid", "only price index benchmarks are supported")
        base = float(series["points"][0]["close"])
        values = []
        for point in series["points"]:
            close = float(point["close"])
            values.append({**point, "normalized": close / base, "return": close / base - 1.0})
        benchmark = {
            "id": series["id"], "snapshot_id": series["snapshot_id"],
            "data_hash": series["snapshot_id"], "start": start, "end": end,
            "return_type": series["return_type"], "points": values,
        }
    if request is not None:
        if result.dataset is None:
            raise BacktestError("run_failed", "result dataset is required for effective config")
        provenance = result.dataset.provenance
        if "effective_config" not in provenance or "config_hash" not in provenance:
            raise BacktestError("run_failed", "prepared config evidence is missing")
        effective_config = dict(provenance["effective_config"])
        value.update({
            "parameters": effective_config["params"],
            "effective_config": effective_config,
            "config_hash": provenance["config_hash"],
        })
        if benchmark is not None:
            value["benchmark"] = benchmark
    if result.account_result is None:
        return value
    points = [(point.day, float(point.equity)) for point in result.account_result.equity
              if point.day >= result.first_at.date()]
    value.update({
        "equity": [{"at": day.isoformat(), "equity": equity} for day, equity in points],
        "trades": _json_value(result.account_result.fills),
        "trades_total": len(result.account_result.fills),
        "trace": _json_value(result.account_result),
        "provenance": _json_value(result.dataset.provenance),
        "scope": _json_value(result.dataset.scope),
    })
    value.update(_json_value(result.dataset.provenance))
    if benchmark is not None:
        value["provenance"] = {
            **value["provenance"],
            "benchmark": {
                key: benchmark[key]
                for key in ("id", "snapshot_id", "data_hash", "start", "end", "return_type")
            },
        }
    return value


__all__ = [
    "BacktestError", "Benchmarks", "CommandPort", "KernelBacktestPort", "Modules", "OperationSpec", "PortResult",
    "Protocol", "PROTOCOL_NAME", "PROTOCOL_VERSION", "export_operations",
    "build_backend", "build_protocol", "build_data", "DataBus", "DataPort", "DataRef", "DataError",
    "Scalar", "Side", "Bar", "Order", "Fill", "DataRequest", "HistoryBar", "MarketFrame", "TargetSet",
    "AccountSnapshot", "RegistrationAction", "HistoryDay", "RegistrationRecord", "HistoryResult",
    "BacktestRequest", "BacktestResponse", "Research", "Trial", "SearchResponse", "parse_backtest_request", "DataQuery", "Dataset",
    "validate_execution", "build_execution_hash",
    "EXECUTION_FIELDS", "required_fields",
]
