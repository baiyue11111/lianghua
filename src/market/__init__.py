"""Standard data access, publication and source inspection."""
from __future__ import annotations

import duckdb
import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from hashlib import sha256
from market.reader import _prepare_dataset
from pathlib import Path
from protocol import DataError, DataQuery, DataRef, Dataset, EXECUTION_FIELDS, validate_facts
from threading import local
from typing import Mapping, Protocol as TypingProtocol
from uuid import uuid4

class _DataStore(TypingProtocol):
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None, folder: str = "") -> tuple[DataRef, ...]: ...
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

    def _source_files(self, suffix: str) -> list[tuple[str, Path]]:
        return [(f"{identity}/{ref.name}", self._store.path("sources", identity, ref.name))
                for identity in self._market_storage_ids()
                for ref in self._store.list("sources", identity)
                if ref.name.endswith(suffix) and ref.name.endswith(".parquet")]

    def _source_query(self, files: list[tuple[str, Path]], sql: str):
        if not files:
            return None
        paths = [str(path) for _, path in files]
        connection = duckdb.connect(":memory:")
        try:
            return connection.execute(sql, [paths]).fetchall()
        finally:
            connection.close()

    def _source_columns(self, files: list[tuple[str, Path]]) -> set[str]:
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

    def _source_table_facts(self, files: list[tuple[str, Path]], *, date_field: str = "date",
                          with_fields: bool = True) -> dict:
        if not files:
            return {"rows": 0, "symbols": 0, "first_date": None, "last_date": None,
                    "columns": set(), "field_stats": {}, "missing": True}
        columns = self._source_columns(files)
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
        fact = self._source_table_facts(files, with_fields=False)
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

    def source_facts(self) -> Mapping[str, object]:
        """Aggregate current source facts without using a report or release manifest."""
        with self._store.source_lock():
            self._source_owner_allowed()
            daily_files = self._source_files(".parquet")
            daily_files = [(name, path) for name, path in daily_files
                           if Path(name).parent.name == "daily"]
            tables = {
                "stock_info": self._source_files("stock_info.parquet"),
                "valuation": self._source_files("valuation_factors.parquet"),
                "fundamentals": self._source_files("fundamentals.parquet"),
            }
            source_refs = [(f"{identity}/{ref.name}", ref) for identity in self._market_storage_ids()
                         for ref in self._store.list("sources", identity)]
            facts = {name: self._source_table_facts(files, date_field="quarter")
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
                    "partition_sha256": {name: ref.sha256 for name, ref in source_refs
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
            stock_files = self._source_files("stock_info.parquet")
            latest_files = self._source_files("latest_snapshot.parquet")
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
        facts = self.source_facts()
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
        facts = self.source_facts()
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
        facts = self.source_facts()
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
                          "范围": data_type or "source",
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

    def list(self, kind, identity=None, folder="") -> tuple[DataRef, ...]:
        if kind == "sources":
            return self._source_view('list', identity, folder)
        return self._namespace_access('list', kind, identity, folder)

    def path(self, kind, identity, name) -> Path:
        if kind == "sources":
            return self._source_view('path', identity, name)
        return self._namespace_access('path', kind, identity, name)

    def delete(self, kind, identity, name) -> None:
        if kind == "sources":
            self._source_mutate('delete', identity, name)
            return
        self._namespace_access('delete', kind, identity, name)


def build_data(root: Path) -> DataBus:
    """Explicit composition for offline and backend data access."""
    from storage import DataStore
    return DataBus(DataStore(root))
