"""Compose modules and route structured requests."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from functools import partial
from importlib import import_module
from inspect import getattr_static, signature
from market import build_data
from pathlib import Path
from protocol import (
    BacktestError,
    BacktestRequest,
    BacktestResponse,
    DataError,
    DataPort,
    DataQuery,
    JsonObject,
    JsonValue,
    PROBLEM_SCHEMA,
    PortKind,
    SearchSpec,
    _config_value,
    _decode_backtest_config,
    _identity,
    _json_value,
    _limits,
    _text,
    normalize_security_id,
    parse_backtest_request,
    required_fields,
    Filter,
    TableDef,
    TableQuery,
    TableWrite,
    SQLQuery,
    validate_execution,
)
from typing import Any, Literal, Protocol as TypingProtocol
from uuid import uuid4

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


class ModulePort(TypingProtocol):
    def handle(self, operation: str, data: JsonValue) -> PortResult | JsonValue: ...


@dataclass(frozen=True)
class Modules:
    """协议需要的模块端口。端口实现由运行时装配。"""

    backtests: ModulePort
    research: ModulePort
    sources: ModulePort
    data: ModulePort
    commands: ModulePort

    def port(self, name: PortKind) -> ModulePort:
        return getattr(self, name)


_OPERATIONS: tuple[OperationSpec, ...] = (
    OperationSpec("backtest.strategies", "read", "strategies", "strategies", "collection", "backtests"),
    OperationSpec("backtest.prepare", "read", "preflight", "preflight", "document", "backtests"),
    OperationSpec("backtest.run", "write", "backtest", "backtest-result", "resource", "backtests"),
    OperationSpec("cli.backtest", "write", "cli-backtest", "cli-backtest", "operation", "commands"),
    OperationSpec("collection.describe", "read", "collection-describe", "collection-describe", "document", "sources"),
    OperationSpec("collection.doctor", "write", "collection-doctor", "collection-doctor", "document", "sources"),
    OperationSpec("collection.inspect", "read", "collection-inspect", "collection-inspect", "document", "sources"),
    OperationSpec("collection.run", "write", "collection-run", "collection-run", "operation", "sources"),
    OperationSpec("cli.ingest", "write", "cli-ingest", "cli-ingest", "operation", "commands"),
    OperationSpec("research.runs", "read", "runs", "runs", "collection", "research"),
    OperationSpec("research.run", "read", "run", "run", "resource", "research"),
    OperationSpec("research.benchmarks", "read", "benchmarks", "benchmarks", "collection", "research"),
    OperationSpec("research.benchmark", "read", "benchmark", "benchmark", "resource", "research"),
    OperationSpec("data.research_inputs", "read", "research-inputs", "research-inputs", "document", "data"),
    OperationSpec("collection.sources", "read", "sources", "sources", "collection", "sources"),
    OperationSpec("collection.sync", "write", "sync", "sync", "operation", "sources"),
    OperationSpec("collection.schedule", "write", "schedule", "schedule", "operation", "sources"),
    OperationSpec("collection.job", "read", "job", "job", "resource", "sources"),
    OperationSpec("collection.snapshot", "read", "snapshot", "snapshot", "resource", "sources"),
    OperationSpec("collection.evidence", "read", "evidence", "evidence", "collection", "sources"),
    OperationSpec("data.status", "read", "data-status", "data-status", "resource", "data"),
    OperationSpec("data.search", "read", "data-search", "data-search", "collection", "data"),
    OperationSpec("data.bars", "read", "kline", "kline", "series", "data"),
    OperationSpec("collection.start", "write", "start-fetch", "fetch-job", "operation", "sources"),
    OperationSpec("collection.current", "read", "fetch-job", "fetch-job", "resource", "sources"),
    OperationSpec("collection.stop", "write", "stop-fetch", "stop-fetch", "operation", "sources"),
    OperationSpec("collection.automatic", "read", "continuous-fetch", "continuous-fetch", "resource", "sources"),
    OperationSpec("collection.set_automatic", "write", "set-continuous-fetch", "continuous-fetch", "operation", "sources"),
    OperationSpec("collection.merge", "write", "start-merge", "merge-job", "operation", "sources"),
    OperationSpec("collection.merge_job", "read", "merge-job", "merge-job", "resource", "sources"),
    OperationSpec("data.check", "read", "doctor", "doctor", "document", "data"),
    OperationSpec("collection.repair", "write", "doctor-fix", "doctor-fix", "operation", "sources"),
    OperationSpec("collection.repair_job", "read", "doctor-job", "doctor-job", "resource", "sources"),
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
            from bisect import bisect_right
            from datetime import datetime, time
            from zoneinfo import ZoneInfo
            requested_code = normalize_security_id(spec["code"])
            manifest = self.store.manifest(dataset_id)
            declared = {normalize_security_id(str(code))
                        for code in (manifest.get('scope') or {}).get('symbols') or ()}
            if declared and requested_code not in declared:
                raise ValueError(f'基准 {identity} 需要 {requested_code} 的行情，'
                                 f'数据集 {dataset_id} 未包含；先导入该基准数据，或改用已导入的基准')
            first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
            if spec.get('alignment') not in (None, 'previous_close'):
                raise ValueError('unsupported benchmark date alignment')
            aligned = spec.get('alignment') == 'previous_close'
            observed = None
            if aligned:
                calendar = self.store.calendar(dataset_id)
                sessions = [date.fromisoformat(day) for day in calendar['sessions']]
                source_zone, target_zone = ZoneInfo(spec['timezone']), ZoneInfo(spec['comparison_timezone'])
                source_close, target_close = time.fromisoformat(spec['session_close']), time.fromisoformat(spec['comparison_close'])
                requested = tuple(dates) if dates is not None else tuple(
                    day.isoformat() for day in sessions if first <= day <= last)
                if not requested or tuple(sorted(set(requested))) != requested:
                    raise ValueError('benchmark comparison dates must be ordered and unique')
                if date.fromisoformat(requested[0]) < first or date.fromisoformat(requested[-1]) > last:
                    raise ValueError('benchmark dates exceed requested interval')
                stamps = [datetime.combine(day, source_close, source_zone) for day in sessions]
                observed = []
                for value in requested:
                    day = date.fromisoformat(value)
                    if not any(span['start'] <= value <= span['end'] for span in calendar['coverage']):
                        raise ValueError('benchmark calendar does not cover comparison date')
                    position = bisect_right(stamps, datetime.combine(day, target_close, target_zone)) - 1
                    if position < 0:
                        raise ValueError('benchmark has no completed close before comparison time')
                    observed.append(sessions[position].isoformat())
                first, last = date.fromisoformat(observed[0]), date.fromisoformat(observed[-1])
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
            if aligned:
                by_day = {point['at']: point['close'] for point in points}
                if any(day not in by_day for day in observed):
                    raise ValueError('benchmark observed close is missing')
                points = [{'at': target, 'close': by_day[day], 'observed_at': day}
                          for target, day in zip(requested, observed)]
            elif dates is not None and tuple(item["at"] for item in points) != tuple(dates):
                raise ValueError("基准日期不全或顺序不一致")
            digest = result.provenance.get('data_hash')
            hashes = {identity: digest}
            if spec.get('fx_id'):
                fx_id = spec['fx_id']
                if fx_id == identity or self.specs[fx_id].get('fx_id'):
                    raise ValueError('benchmark FX must be a direct rate series')
                rate_spec = self.specs[fx_id]
                if (rate_spec.get('base_currency') != spec.get('quote_currency')
                        or rate_spec.get('currency') != spec.get('currency')):
                    raise ValueError('benchmark FX currency direction differs')
                fx = self.series(fx_id, start, end, dates=tuple(point['at'] for point in points))
                points = [{**point, 'close': point['close'] * rate['close'],
                           'fx_observed_at': rate.get('observed_at', rate['at'])}
                          for point, rate in zip(points, fx['points'])]
                hashes.update(fx['source_hashes'])
                from hashlib import sha256
                digest = sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            return {**spec, "snapshot_id": digest, 'source_hashes': hashes,
                    "collected_at": None, "start": start, "end": end,
                    "points": points}
        raise ValueError('基准必须登记标准dataset_id')


class Research:
    tables = (
        TableDef('runs', {
            'run_id': 'VARCHAR', 'strategy_id': 'VARCHAR', 'dataset_id': 'VARCHAR',
            'status': 'VARCHAR', 'first_at': 'VARCHAR', 'last_at': 'VARCHAR',
            'steps': 'BIGINT', 'fills': 'BIGINT', 'final_cash': 'VARCHAR',
            'final_equity': 'VARCHAR', 'pending_orders': 'BIGINT',
            'manifest': 'JSON', 'metadata': 'JSON', 'trace_sections': 'JSON', 'result_fields': 'JSON', 'hashes': 'JSON',
        }, ('run_id',)),
        TableDef('run_equity', {'run_id': 'VARCHAR', 'position': 'BIGINT',
                               'at': 'VARCHAR', 'equity': 'DOUBLE'}, ('run_id', 'position'),
                 storage='parquet'),
        TableDef('run_trades', {
            'run_id': 'VARCHAR', 'position': 'BIGINT', 'order_id': 'VARCHAR',
            'symbol': 'VARCHAR', 'trade_date': 'VARCHAR', 'side': 'VARCHAR',
            'status': 'VARCHAR', 'shares': 'BIGINT', 'price': 'DOUBLE',
            'gross_amount': 'DOUBLE', 'commission': 'DOUBLE', 'stamp_duty': 'DOUBLE',
            'transfer_fee': 'DOUBLE', 'reason': 'VARCHAR',
        }, ('run_id', 'position'), storage='parquet'),
        TableDef('run_positions', {'run_id': 'VARCHAR', 'symbol': 'VARCHAR',
                                  'shares': 'VARCHAR'}, ('run_id', 'symbol'),
                 storage='parquet'),
        TableDef('run_events', {'run_id': 'VARCHAR', 'section': 'VARCHAR',
                               'position': 'BIGINT', 'value': 'JSON'},
                 ('run_id', 'section', 'position'), storage='parquet'),
        TableDef('run_trials', {'run_id': 'VARCHAR', 'position': 'BIGINT',
                               'trial_id': 'VARCHAR', 'extra': 'JSON', 'removed': 'JSON'},
                 ('run_id', 'position'), storage='parquet'),
        TableDef('run_failures', {'run_id': 'VARCHAR', 'code': 'VARCHAR',
                                 'detail': 'VARCHAR', 'partial': 'JSON'}, ('run_id',)),
    )

    def __init__(self, data):
        self.data = data
        for table in self.tables:
            data.define(table)

    def rows(self, name, identity=None, *, order=()):
        filters = (Filter('run_id', 'eq', identity),) if identity is not None else ()
        rows = self.data.scan(TableQuery(name, filters, order=order)).table.to_pylist()
        definition = next(table for table in self.tables if table.name == name)
        for row in rows:
            for column, kind in definition.columns.items():
                if kind == 'JSON' and row[column] is not None:
                    row[column] = json.loads(row[column])
        return rows

    def write(self, name, rows):
        import pyarrow as pa
        definition = next(table for table in self.tables if table.name == name)
        if not rows:
            return
        encoded = [{column: json.dumps(row[column], ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':'), allow_nan=False)
                    if kind == 'JSON' and row[column] is not None else row[column]
                    for column, kind in definition.columns.items()} for row in rows]
        self.data.write(TableWrite(name, pa.Table.from_pylist(encoded)))

    def _ids(self):
        return tuple(row['run_id'] for row in self.rows('runs'))

    def result(self, identity):
        rows = self.rows('runs', identity)
        if not rows:
            raise KeyError(identity)
        row = rows[0]
        value = dict(row['metadata'])
        value.update({key: row[key] for key in (
            'run_id', 'strategy_id', 'dataset_id', 'first_at', 'last_at', 'steps',
            'fills', 'final_cash', 'final_equity', 'pending_orders')})
        value['id'] = identity
        value['positions'] = {item['symbol']: item['shares'] for item in self.rows('run_positions', identity)}
        value['equity'] = [{key: item[key] for key in ('at', 'equity')}
                           for item in self.rows('run_equity', identity, order=('position',))]
        value['trades'] = [{key: item[key] for key in item if key not in {'run_id', 'position'}}
                           for item in self.rows('run_trades', identity, order=('position',))]
        value['trace'] = {section: [] for section in row['trace_sections']}
        for item in self.rows('run_events', identity, order=('section', 'position')):
            value['trace'][item['section']].append(item['value'])
        if 'search' in value:
            trials = []
            for item in self.rows('run_trials', identity, order=('position',)):
                trial = self.result(item['trial_id'])
                for key in item['removed']:
                    trial.pop(key, None)
                trial.update(item['extra'])
                trials.append(trial)
            value['search'] = {**value['search'], 'trials': trials}
        return {key: item for key, item in value.items() if key in row['result_fields']}

    def _read(self, identity, *, include_result=True):
        rows = self.rows('runs', identity)
        if not rows:
            raise KeyError(identity)
        row = rows[0]
        manifest, metrics = row['manifest'], row['metadata']['metrics']
        return {
            "id": identity, "run_id": manifest["run_id"],
            "strategy_id": manifest["strategy_id"], "status": manifest["status"],
            "qualification": "unverified", "limitations": manifest.get("limitations", []),
            "metrics": metrics, "manifest_file": f"lake:runs/{identity}",
            "manifest_sha256": row['hashes']['manifest'],
            "metrics_sha256": row['hashes']['metrics'],
            "source_hashes": manifest.get("source_hashes", {}),
            "config_hash": manifest.get("config_hash"),
            "note": manifest.get("note", "运行结果未经历史资格认定。"),
            "parameters": manifest.get("parameters", {}),
            "effective_config": manifest.get("effective_config", {}),
            "code_hash": manifest.get("code_hash"), "data_hash": manifest.get("data_hash"),
            "execution_hash": manifest.get("execution_hash"), "result": self.result(identity) if include_result else None,
        }

    def list(self):
        return [self._read(identity) for identity in self._ids()]

    def detail(self, identity, offset=0, limit=50):
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise ValueError('offset and limit must be non-negative integers')
        with self.data.transaction(readonly=True):
            value = self._read(identity, include_result=False)
            value.pop('result')
            row = self.rows('runs', identity)[0]
            manifest = row['manifest']
            filters = (Filter('run_id', 'eq', identity),)
            trades = self.data.scan(TableQuery('run_trades', filters, order=('position',),
                                                limit=limit, offset=offset)).table.to_pylist()
            trades = [{key: item for key, item in trade.items() if key not in {'run_id', 'position'}} for trade in trades]
            total = self.data.scan(SQLQuery('SELECT count(*) AS n FROM run_trades WHERE run_id=?', (identity,))).table.to_pylist()[0]['n']
            count = self.data.scan(SQLQuery('SELECT count(*) AS n FROM run_equity WHERE run_id=?', (identity,))).table.to_pylist()[0]['n']
            stride = max(1, (count + 799) // 800)
            equity = self.data.scan(SQLQuery('SELECT "at",equity FROM run_equity WHERE run_id=? '
                'AND (position % ? = 0 OR position=?) ORDER BY position', (identity, stride, count - 1))).table.to_pylist()
            value['trades_total'] = total
            value['equity_sampling'] = {'total': count, 'stride': stride}
            evidence = [{'file': reference, 'sha256': row['hashes'][name]} for name, reference in (
                ('manifest', f'lake:runs/{identity}#manifest'), ('metrics', f'lake:runs/{identity}#metrics'),
                ('result', f'lake:runs/{identity}'), ('trace', f'lake:run_events/{identity}'))]
        return {**value, "manifest": manifest, "trades": trades,
                "trades_total": value["trades_total"], "equity": equity,
                "evidence": evidence, "offset": offset, "limit": limit}


def build_protocol(modules: Modules) -> Protocol:
    """用显式模块端口组装唯一协议入口。"""

    return Protocol(modules)


def save_backtest(data: DataPort, request: BacktestRequest, value: Mapping[str, Any]) -> None:
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
    research = Research(data)
    core = ('run_id', 'strategy_id', 'dataset_id', 'first_at', 'last_at', 'steps',
            'fills', 'final_cash', 'final_equity', 'pending_orders')
    excluded = {*core, 'id', 'equity', 'trades', 'trace', 'positions'}
    metadata = {key: item for key, item in value.items() if key not in excluded}
    trace = value.get('trace', {})
    if not isinstance(trace, Mapping) or any(not isinstance(items, list) for items in trace.values()):
        raise TypeError('account trace must contain event lists')
    trials = []
    if 'search' in metadata:
        search = dict(metadata['search'])
        trials = search.pop('trials')
        metadata['search'] = search
    run_id = request.run_id
    row = {key: value[key] for key in core}
    from hashlib import sha256
    hashes = {name: sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
               separators=(',', ':'), allow_nan=False).encode()).hexdigest()
              for name, item in (('manifest', manifest), ('metrics', value['metrics']),
                                 ('result', value), ('trace', trace))}
    row.update(status='succeeded', manifest=manifest, metadata=metadata,
               trace_sections=list(trace), result_fields=list(value), hashes=hashes)
    with data.transaction():
        research.write('runs', [row])
        research.write('run_equity', [dict(run_id=run_id, position=index, **item)
                                     for index, item in enumerate(value.get('equity', []))])
        research.write('run_trades', [dict(run_id=run_id, position=index, **item)
                                     for index, item in enumerate(value.get('trades', []))])
        research.write('run_positions', [dict(run_id=run_id, symbol=symbol, shares=shares)
                                        for symbol, shares in value['positions'].items()])
        research.write('run_events', [dict(run_id=run_id, section=section, position=index, value=item)
                                     for section, items in trace.items()
                                     for index, item in enumerate(items)])
        links = []
        for index, trial in enumerate(trials):
            stored = research.result(trial['run_id'])
            links.append(dict(run_id=run_id, position=index, trial_id=trial['run_id'],
                              extra={key: item for key, item in trial.items()
                                     if key not in stored or item != stored[key]},
                              removed=[key for key in stored if key not in trial]))
        research.write('run_trials', links)


def save_failed_backtest(data: DataPort, run_id: str, error: Exception,
                         partial: object | None) -> str:
    value = {"run_id": run_id, "code": getattr(error, "code", "run_failed"),
             "detail": str(error), "partial": _json_value(partial) if partial is not None else None}
    Research(data).write('run_failures', [value])
    return f"lake:run_failures/{run_id}"


class CollectionPort:
    def __init__(self, collect):
        self.collect = collect
        self.data = collect.data

    def start(self):
        self.collect.start()

    def close(self):
        self.collect.close()

    def handle(self, operation, value):
        if operation == 'collection.describe':
            return self.collect.sources.describe()
        if operation == 'collection.run':
            return self.collect.run(value)
        if operation == 'collection.doctor':
            return self.collect.methods.doctor(self.collect.sources.get(value['source_id']), value)
        if operation == 'collection.inspect':
            source = self.collect.sources.get(value['source_id'])
            return self.collect.methods.inspect(source, value)
        # Existing source operations retain their registered structured contract.
        return self.collect.handle(operation, value)





def build_backend(root: Path) -> Protocol:
    """Compose the project once from the protocol data registry."""

    from collect.core import Collect
    from collect.methods import Methods
    from collect.sources import Sources
    from market.views import Views

    project = Path(root).resolve()
    data = build_data(project)
    sources = Sources(data)
    registry = sources.describe()
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
    commands = CommandPort(project, backtests, data)
    return Protocol(Modules(backtests, backtests, CollectionPort(sources_port), Views(data, sources), commands))


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
        if operation == "backtest.strategies":
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
        if operation == "backtest.prepare":
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
        if operation == "research.runs":
            return {"items": self._research.list()}
        if operation == "research.run":
            if not isinstance(data, dict):
                raise TypeError("operation data must be an object")
            return self._research.detail(data["id"], data.get("offset", 0), data.get("limit", 50))
        if operation == "research.benchmarks":
            return self._benchmarks.list()
        if operation == "research.benchmark":
            if not isinstance(data, dict):
                raise TypeError("operation data must be an object")
            return self._benchmarks.series(data["id"], data["start"], data["end"])
        if operation != "backtest.run":
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
            "urn:lianghua:ui:output:backtest.run:v1", value,
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
                    result = self._backtests.handle("backtest.run", request)
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
                                  "result": {key:item for key,item in value.items()
                                             if key not in {'trace','trades'}}})
            value = {"run_id": batch_id, "intervals": completed}
            return PortResult(
                "resource", "lianghua:cli-backtest",
                "urn:lianghua:ui:output:cli.backtest:v1", value,
            )
        if operation == "cli.ingest":
            from market.ingest import ingest_exploratory

            identity = _identity(data, "output")
            result = ingest_exploratory(
                price_workbook=Path(_text(data, "price")),
                fundamental_workbook=Path(_text(data, "fundamentals")),
                valuation_workbook=Path(_text(data, "valuations")),
                data=self._data,
                dataset=identity,
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
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("run_id must be a non-empty text identity without control characters")


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
            self._modules.data, self._modules.commands,
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
        tax=(request.settlement_parameters or {}).get("execution", {}).get("tax", {}).get("model",
            "none" if request.settlement_parameters.get('accounting') == 'research' else "fifo"),
        accounting=request.settlement_parameters.get('accounting','account'),
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
        if series.get("return_type") not in {"price", "total"}:
            raise BacktestError("benchmark_invalid", "benchmark return type must be price or total")
        base = float(series["points"][0]["close"])
        values = []
        for point in series["points"]:
            close = float(point["close"])
            values.append({**point, "normalized": close / base, "return": close / base - 1.0})
        benchmark = {
            "id": series["id"], "snapshot_id": series["snapshot_id"],
            "data_hash": series["snapshot_id"], "start": start, "end": end,
            "return_type": series["return_type"], "points": values,
            **{key: series[key] for key in ("currency", "quote_currency", "source_hashes", "alignment") if key in series},
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
