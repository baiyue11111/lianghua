"""唯一的后端协议入口。

这个文件集中定义外部操作和数据读写协议，把请求分发到模块端口，
把结果转换成 ``frontend/ui-protocol.py`` 的消息。Kernel 只在这里被接入一次。
HTTP、CLI、任务线程和前端组件都不属于本层。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from importlib import import_module
from functools import partial
from inspect import getattr_static, signature
from pathlib import Path
from hashlib import sha256
import json
import subprocess
from types import MappingProxyType
from typing import Literal, Mapping, Protocol as TypingProtocol, TypeAlias, TYPE_CHECKING
from uuid import uuid4

from core.backtest import BacktestError, BacktestRequest, BacktestResponse
from core.kernel import Kernel
from application import save_backtest, save_failed_backtest

ui = import_module("frontend.ui-protocol")

if TYPE_CHECKING:
    from account_history import HistoryDay

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
PortKind: TypeAlias = Literal["backtests", "research", "sources", "lake", "commands"]

PROTOCOL_NAME = ui.PROTOCOL_NAME
PROTOCOL_VERSION = ui.PROTOCOL_VERSION
PROBLEM_SCHEMA = "urn:lianghua:ui:problem:v1"
EXECUTION_FIELDS = ("open", "high", "low", "close", "volume", "amount")


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
        if self.price_basis != "adjusted":
            raise ValueError("unsupported query price basis")
        if not isinstance(self.universe, str) or not self.universe.strip():
            raise ValueError("universe is required")
        if self.symbols is not None:
            symbols = tuple(self.symbols)
            object.__setattr__(self, "symbols", symbols)
        if self.symbols is not None and (not self.symbols or
                any(not isinstance(symbol, str) or not symbol.strip() for symbol in self.symbols) or
                len(set(self.symbols)) != len(self.symbols)):
            raise ValueError("symbols must be ordered and unique")


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
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
    def delete(self, kind: str, identity: str, name: str) -> None: ...
    def snapshot(self, dataset: str) -> Mapping[str, object]: ...
    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef: ...


class DataBus:
    """All business persistence enters this standard protocol boundary."""
    kinds = frozenset({'sources', 'runs', 'state', 'validation'})

    def __init__(self, store: DataPort, reader=None):
        self._store = store
        self._reader = reader

    def query(self, query: DataQuery) -> Dataset:
        if not isinstance(query, DataQuery):
            raise DataError("invalid_query", "query must be DataQuery")
        if self._reader is None:
            raise DataError("reader_unconfigured", "dataset reader is not configured")
        try:
            result = self._reader.query(query)
        except DataError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise DataError("invalid_query", str(error)) from error
        if not isinstance(result, Dataset):
            raise DataError("invalid_dataset", "reader must return Dataset")
        if result.dataset != query.dataset:
            raise DataError("dataset_mismatch", "reader returned a different dataset")
        if result.scope.get("universe") is None:
            raise DataError("invalid_dataset", "dataset scope must declare universe")
        if query.universe != "dataset" and result.scope.get("universe") != query.universe:
            raise DataError("scope_mismatch", "dataset universe does not satisfy query")
        if query.symbols is not None and tuple(result.scope.get("symbols", ())) != query.symbols:
            raise DataError("scope_mismatch", "dataset symbols do not satisfy query")
        if tuple(result.fields) != tuple(query.fields):
            raise DataError("field_mismatch", "dataset fields do not satisfy query")
        if result.history != query.history:
            raise DataError("history_mismatch", "dataset history does not satisfy query")
        last = result.records[-1].trade_date
        following = result.calendar[-1]
        if not last <= query.end < following:
            raise DataError("date_coverage", "dataset must cover the query end before its following-session sentinel")
        return result

    def snapshot(self, dataset: str) -> Mapping[str, object]:
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

    def publish(self, dataset: str, manifest: Mapping[str, object]) -> DataRef:
        """Validate all bound evidence, then publish the manifest as the release marker."""
        self._validate_manifest(dataset, manifest)
        content = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode()
        return self.write("sources", dataset, "manifest.json", content)

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
            if not isinstance(ref, Mapping) or set(ref) != {"file", "sha256", "role"}:
                raise DataError("invalid_manifest", "dataset evidence references must declare file, sha256 and role")
            name, digest, role = ref["file"], ref["sha256"], ref["role"]
            if (not isinstance(name, str) or not name or Path(name).is_absolute()
                    or ".." in Path(name).parts or name in names):
                raise DataError("invalid_manifest", "dataset evidence files must be relative and unique")
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    or role not in {"market", "account"}):
                raise DataError("invalid_manifest", f"invalid dataset evidence reference: {name}")
            names.add(name)
            try:
                value = self.read("sources", dataset, name)
            except DataError as error:
                raise DataError("invalid_manifest", f"unbound dataset evidence: {name}") from error
            if sha256(value).hexdigest() != digest:
                raise DataError("invalid_manifest", f"dataset evidence hash mismatch: {name}")

    def configure_reader(self, reader) -> None:
        if self._reader is not None:
            raise ValueError("dataset reader is already configured")
        if not callable(getattr(reader, "query", None)):
            raise TypeError("reader must provide query")
        self._reader = reader

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
        return self._call('read', kind, identity, name)

    def write(self, kind, identity, name, value: bytes) -> DataRef:
        return self._call('write', kind, identity, name, value)

    def list(self, kind, identity=None) -> tuple[DataRef, ...]:
        return self._call('list', kind, identity)

    def path(self, kind, identity, name) -> Path:
        return self._call('path', kind, identity, name)

    def delete(self, kind, identity, name) -> None:
        self._call('delete', kind, identity, name)


def build_data(root: Path, reader=None) -> DataBus:
    """Explicit composition for both the backend and offline operations."""
    from storage import DataStore
    return DataBus(DataStore(root), reader)


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
    validation: BacktestRequest | None
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
    OperationSpec("cli.run", "write", "cli-run", "cli-run", "operation", "commands"),
    OperationSpec("workbench.runs", "read", "runs", "runs", "collection", "research"),
    OperationSpec("workbench.run", "read", "run", "run", "resource", "research"),
    OperationSpec("workbench.benchmarks", "read", "benchmarks", "benchmarks", "collection", "research"),
    OperationSpec("workbench.benchmark", "read", "benchmark", "benchmark", "resource", "research"),
    OperationSpec("lake.researchDaily", "read", "research-daily", "research-daily", "series", "lake"),
    OperationSpec("lake.researchInputs", "read", "research-inputs", "research-inputs", "document", "lake"),
    OperationSpec("lake.closedLoop", "read", "closed-loop", "closed-loop", "document", "lake"),
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
    OperationSpec("cli.report", "read", "cli-report", "cli-report", "document", "commands"),
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


def build_protocol(modules: Modules) -> Protocol:
    """用显式模块端口组装唯一协议入口。"""

    return Protocol(modules)


def build_backend(root: Path) -> Protocol:
    """Compose the project once; adapters receive only the returned backend port."""

    from collect.lake_display import LakePort
    from workbench.port import WorkbenchPort
    from collect.display import CollectPort
    from collect.data import Store

    project = Path(root).resolve()
    config = json.loads((project / "configs/workbench.json").read_bytes())
    data = build_data(project)
    history_settings = config.get("backtests", {})
    from history_data import HistoricalLake
    data.configure_reader(HistoricalLake(data, history_settings))
    settlement_type = _load_plugin(history_settings, "settlement")
    store = Store(data)
    providers = {
        item['id']: getattr(import_module(item['adapter'].split(':', 1)[0]),
                            item['adapter'].split(':', 1)[1])(item)
        for item in config['sources']
    }
    sources = CollectPort(config['sources'], store, providers)
    workbench = WorkbenchPort(project, config, data, store=store)
    backtests = BacktestModule(workbench, KernelBacktestPort(data, settlement_type, workbench.benchmarks))
    lake = LakePort(data)
    commands = CommandPort(project, backtests, data)
    return Protocol(Modules(backtests, workbench, sources, lake, commands))


class KernelBacktestPort:
    """把固定 Kernel 适配成统一协议中的一个端口。"""

    def __init__(self, data: DataPort, settlement_type, benchmarks=None) -> None:
        if not callable(settlement_type):
            raise TypeError("settlement plugin must be callable")
        self._kernel = Kernel(data, _load_registered_strategy, _query, settlement_type)
        self._data = data
        self._benchmarks = benchmarks

    def handle(self, operation: str, data: JsonValue) -> PortResult:
        if operation != "workbench.backtest":
            raise BacktestError("unsupported_operation", operation)
        request = _request(data)
        search = _search(data, request)
        try:
            if search is None:
                result = self._kernel.run(request)
            else:
                result = self._kernel.search(
                    request, search.updater, max_trials=search.max_trials,
                    metric=search.metric, direction=search.direction,
                    validation=search.validation,
                )
        except BacktestError as error:
            if error.code == "busy":
                raise
            if search is None:
                partial = getattr(error, "partial_result", None)
                if partial is not None:
                    error.evidence_ref = save_failed_backtest(self._data, request, error, partial)
            else:
                error.evidence_ref = self._save_search_failure(request, error)
            raise
        if search is None:
            value = _result_data(result, request, self._benchmarks)
            save_backtest(self._data, request, value)
        else:
            value = self._save_search(request, search, result)
        return PortResult(
            "resource", "lianghua:backtest-result",
            "urn:lianghua:ui:output:workbench.backtest:v1", value,
        )

    def _save_search_failure(self, request: BacktestRequest, error: BacktestError) -> str:
        completed = getattr(error, "completed_trials", ())
        for trial in completed:
            save_backtest(self._data, trial.request, _result_data(trial.response, trial.request, self._benchmarks))
        from plugins.recorders.jsonl import _json_value
        failed_request = getattr(error, "failed_request", request)
        partial = getattr(error, "partial_result", None)
        evidence = {
            "completed_trials": [
                _result_data(trial.response, trial.request, self._benchmarks) for trial in completed
            ],
            "failed_request": _json_value(failed_request),
            "partial": _json_value(partial) if partial is not None else None,
        }
        if hasattr(error, "failed_values"):
            evidence["failed_values"] = _json_value(error.failed_values)
        if hasattr(error, "trial_index"):
            evidence["trial_index"] = error.trial_index
        return save_failed_backtest(self._data, request, error, evidence)

    def _save_search(self, request: BacktestRequest, spec: SearchSpec, result) -> JsonObject:
        from strategy import canonical_hash

        trials = []
        for trial in result.trials:
            value = _result_data(trial.response, trial.request, self._benchmarks)
            value["score"] = trial.score
            trials.append(value)
            save_backtest(self._data, trial.request, value)
        best = result.trials[result.best_index]
        value = dict(_result_data(best.response, best.request, self._benchmarks))
        validation_value = None
        if result.validation is not None:
            validation_request = replace(
                spec.validation,
                run_id=f"{request.run_id}-validation",
                strategy_id=request.strategy_id,
                dataset_id=request.dataset_id,
                parameters=dict(best.request.parameters),
            )
            validation_value = _result_data(result.validation, validation_request, self._benchmarks)
            save_backtest(self._data, validation_request, validation_value)
        effective_config = dict(value["effective_config"])
        declaration = dict(spec.declaration)
        declaration["base_parameters"] = dict(request.parameters)
        effective_config["search"] = declaration
        value.update({
            "id": request.run_id,
            "run_id": request.run_id,
            "parameters": dict(best.request.parameters),
            "effective_config": effective_config,
            "config_hash": canonical_hash({
                "strategy_id": request.strategy_id,
                "parameters": dict(best.request.parameters),
                **effective_config,
            }),
            "search": {
                "metric": spec.metric,
                "direction": spec.direction,
                "best_run_id": best.request.run_id,
                "best_index": result.best_index,
                "trials": trials,
                "validation": validation_value,
            },
        })
        summary_request = replace(request, parameters=dict(best.request.parameters))
        save_backtest(self._data, summary_request, value)
        return value


class BacktestModule:
    """Catalog and preflight share a port; execution always enters Kernel."""

    def __init__(self, workbench: ModulePort, kernel: KernelBacktestPort) -> None:
        self._workbench = workbench
        self._kernel = kernel

    def handle(self, operation: str, data: JsonValue) -> PortResult | JsonValue:
        if operation == "workbench.backtest":
            return self._kernel.handle(operation, data)
        if operation in {"workbench.strategies", "workbench.preflight"}:
            return self._workbench.handle(operation, data)
        raise ValueError(f"unsupported backtest operation: {operation}")


class CommandPort:
    """CLI transport operations; no command owns a business execution path."""

    def __init__(self, root: Path, backtests: ModulePort, data: DataPort) -> None:
        self._root = Path(root).resolve()
        self._backtests = backtests
        self._data = data

    def handle(self, operation: str, data: JsonValue) -> JsonValue:
        if not isinstance(data, dict):
            raise TypeError("operation data must be an object")
        if operation == "cli.run":
            config = json.loads(Path(_text(data, "config")).read_bytes())
            return self._backtests.handle("workbench.backtest", config["request"])
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
        if operation == "cli.report":
            output = self._data.path("runs", _identity(data, "output"), "report.html")
            output.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([
                "node", _text(data, "builder"), "--input", _text(data, "artifact"),
                "--output", str(output), "--ready-timeout-ms",
                str(data["ready_timeout_ms"]), "--timeout-ms", str(data["timeout_ms"]),
            ], check=True)
            return {"output": str(output)}
        raise ValueError(f"unsupported command operation: {operation}")


class Protocol:
    """后端唯一入口；每个请求只经过一个已声明的模块端口。"""

    def __init__(self, modules: Modules) -> None:
        self._modules = modules

    async def send(self, request: ui.UiRequest) -> list[ui.UiMessage]:
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
                 retryable: bool = False, evidence: str | None = None) -> ui.UiMessage:
        problem = ui.UiProblem(
            type="urn:lianghua:problem:protocol", code=code,
            title="协议请求未完成", detail=detail, retryable=retryable,
            extensions={} if evidence is None else {"x-evidence": {"href": evidence}},
        )
        return ui.UiMessage(
            message_id=uuid4().hex, correlation_id=correlation_id,
            kind="problem", type=problem.type, schema=PROBLEM_SCHEMA, data=problem.to_dict(),
        )


def _request(data: JsonValue) -> BacktestRequest:
    if not isinstance(data, dict):
        raise BacktestError("invalid_request", "backtest data must be an object")
    try:
        allowed = {"run_id", "module", "params", "dataset_id", "start", "end", "initial_cash",
                   "fields", "history", "price_basis", "as_of", "symbols", "universe", "limits", "evaluation", "benchmark_id",
                   "search"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unsupported request fields: {', '.join(sorted(unknown))}")
        settlement_parameters = {}
        if data.get("limits") is not None:
            settlement_parameters["limits"] = _limits(data["limits"])
        return BacktestRequest(
            run_id=_text(data, "run_id") if "run_id" in data else uuid4().hex,
            strategy_id=_text(data, "module"), parameters=_object(data, "params"),
            dataset_id=_text(data, "dataset_id"),
            start=_instant(data, "start", end=False),
            end=_instant(data, "end", end=True),
            initial_cash=Decimal(_text(data, "initial_cash")),
            fields=_names(data.get("fields", ()), "fields"), history=data.get("history", 0),
            price_basis=data.get("price_basis", "adjusted"),
            as_of=date.fromisoformat(data["as_of"]) if data.get("as_of") else None,
            symbols=_names(data["symbols"], "symbols") if data.get("symbols") is not None else None,
            universe=data.get("universe", "dataset"),
            settlement_parameters=settlement_parameters,
            evaluation_parameters=_object(data, "evaluation") if "evaluation" in data else {},
            benchmark_id=data.get("benchmark_id"),
        )
    except BacktestError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BacktestError("invalid_request", str(error)) from error


def _search(data: JsonValue, request: BacktestRequest) -> SearchSpec | None:
    if not isinstance(data, dict) or "search" not in data:
        return None
    value = data["search"]
    if not isinstance(value, dict):
        raise BacktestError("invalid_search", "search must be an object")
    allowed = {"updater", "max_trials", "metric", "direction", "validation"}
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
    validation = None
    if "validation" in value:
        raw = value["validation"]
        if not isinstance(raw, dict) or set(raw) != {"start", "end"}:
            raise BacktestError("invalid_search", "validation must contain only start and end")
        validation = _validation_request(request, raw, request.parameters)
    updater = _updater(value["updater"])
    declaration = {
        "updater": value["updater"], "max_trials": max_trials,
        "metric": metric, "direction": direction,
    }
    if "validation" in value:
        declaration["validation"] = dict(value["validation"])
    return SearchSpec(updater, max_trials, metric, direction, validation, declaration)


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


def _validation_request(request: BacktestRequest, value: Mapping, parameters: Mapping) -> BacktestRequest:
    if not isinstance(value, Mapping):
        raise BacktestError("invalid_search", "validation must be an object")
    try:
        start = _instant(value, "start", end=False)
        end = _instant(value, "end", end=True)
        return BacktestRequest(
            run_id=request.run_id, strategy_id=request.strategy_id, parameters=dict(parameters),
            dataset_id=request.dataset_id, start=start, end=end,
            initial_cash=request.initial_cash, fields=request.fields, history=request.history,
            price_basis=request.price_basis, as_of=request.as_of, symbols=request.symbols,
            universe=request.universe, settlement_parameters=request.settlement_parameters,
            evaluation_parameters=request.evaluation_parameters, benchmark_id=request.benchmark_id,
        )
    except BacktestError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BacktestError("invalid_search", str(error)) from error


def _text(data: Mapping[str, JsonValue], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _names(value, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an ordered list")
    return tuple(value)


def _limits(value):
    from account_allocation import AllocationLimits
    if not isinstance(value, dict) or set(value) - {"lot_size", "stock_weight", "adv_fraction"}:
        raise ValueError("unsupported settlement limits")
    defaults = AllocationLimits()
    lot_size = value.get("lot_size", defaults.lot_size)
    if type(lot_size) is not int or lot_size <= 0:
        raise ValueError("lot_size must be a positive integer")
    parsed = {"lot_size": lot_size}
    for name in ("stock_weight", "adv_fraction"):
        raw = value.get(name, getattr(defaults, name))
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
    symbols = request.symbols if request.symbols is not None else requirements.symbols
    if requirements.symbols is not None and symbols != requirements.symbols:
        raise BacktestError("invalid_query", "custom symbols conflict with strategy scope")
    if (requirements.universe != "dataset" and request.universe != "dataset"
            and request.universe != requirements.universe):
        raise BacktestError("invalid_query", "custom universe cannot lower or replace strategy universe")
    universe = request.universe if request.universe != "dataset" else requirements.universe
    return DataQuery(
        dataset=request.dataset_id, start=request.start.date(), end=request.end.date(),
        fields=fields, history=history, price_basis=request.price_basis,
        as_of=request.as_of, symbols=symbols, universe=universe,
        frequency=requirements.frequency,
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
        return merged

    def describe(overrides):
        return {"module": name, "params": _config_value(merge(overrides))}

    configured.describe = describe
    return configured


def _config_value(value):
    if is_dataclass(value):
        return {key: _config_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _config_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_config_value(item) for item in value]
    return value


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
        from strategy import canonical_hash
        plugin_config = {
            key: result.dataset.provenance[key]
            for key in ("settlement", "signal_plugins")
            if key in result.dataset.provenance
        }
        effective_config = {
            "dataset_id": request.dataset_id, "start": request.start.isoformat(),
            "end": request.end.isoformat(), "initial_cash": str(request.initial_cash),
            "fields": list(result.dataset.fields) if result.dataset is not None else list(request.fields),
            "history": request.history,
            "price_basis": request.price_basis, "as_of": request.as_of.isoformat() if request.as_of else None,
            "symbols": list(request.symbols) if request.symbols else None,
            "universe": request.universe, "evaluation": dict(request.evaluation_parameters), **plugin_config,
        }
        effective_config["benchmark_id"] = request.benchmark_id
        if benchmark is not None:
            effective_config["benchmark"] = {
                key: benchmark[key] for key in ("id", "snapshot_id", "data_hash", "start", "end", "return_type")
            }
        value.update({
            "parameters": dict(request.parameters),
            "effective_config": effective_config,
            "config_hash": canonical_hash({
                "strategy_id": request.strategy_id,
                "parameters": dict(request.parameters),
                **effective_config,
            }),
        })
        if benchmark is not None:
            value["benchmark"] = benchmark
    if result.account_result is None:
        return value
    from plugins.recorders.jsonl import _json_value
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
    return value


__all__ = [
    "BacktestError", "BacktestModule", "CommandPort", "KernelBacktestPort", "Modules", "OperationSpec", "PortResult",
    "Protocol", "PROTOCOL_NAME", "PROTOCOL_VERSION", "export_operations",
    "build_backend", "build_protocol", "build_data", "DataBus", "DataPort", "DataRef", "DataError",
    "DataQuery", "Dataset",
    "EXECUTION_FIELDS", "required_fields",
]
