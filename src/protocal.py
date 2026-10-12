"""唯一的后端协议入口。

这个文件集中定义外部操作和数据读写协议，把请求分发到模块端口，
把结果转换成 ``frontend/ui-protocol.py`` 的消息。Kernel 只在这里被接入一次。
HTTP、CLI、任务线程和前端组件都不属于本层。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from importlib import import_module
from pathlib import Path
import json
import subprocess
from typing import Literal, Mapping, Protocol as TypingProtocol, TypeAlias
from uuid import uuid4

from core.backtest import BacktestError, BacktestRequest, BacktestResponse
from core.interfaces import RunFactory
from core.kernel import Kernel
from application import save_backtest

ui = import_module("frontend.ui-protocol")

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
PortKind: TypeAlias = Literal["backtests", "research", "sources", "lake", "commands"]

PROTOCOL_NAME = ui.PROTOCOL_NAME
PROTOCOL_VERSION = ui.PROTOCOL_VERSION
PROBLEM_SCHEMA = "urn:lianghua:ui:problem:v1"


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


class DataPort(TypingProtocol):
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
    def delete(self, kind: str, identity: str, name: str) -> None: ...


class DataBus:
    """All business persistence enters this standard protocol boundary."""
    kinds = frozenset({'sources', 'runs', 'state', 'validation'})

    def __init__(self, store: DataPort):
        self._store = store

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


def build_data(root: Path) -> DataBus:
    """Explicit composition for both the backend and offline operations."""
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


def build_backend(root: Path, factory: RunFactory) -> Protocol:
    """Compose the project once; adapters receive only the returned backend port."""

    from collect.lake_display import LakePort
    from workbench.port import WorkbenchPort
    from collect.display import CollectPort
    from collect.data import Store

    project = Path(root).resolve()
    config = json.loads((project / "configs/workbench.json").read_bytes())
    data = build_data(project)
    store = Store(data)
    providers = {
        item['id']: getattr(import_module(item['adapter'].split(':', 1)[0]),
                            item['adapter'].split(':', 1)[1])(item)
        for item in config['sources']
    }
    sources = CollectPort(config['sources'], store, providers)
    workbench = WorkbenchPort(project, config, data, store=store)
    backtests = BacktestModule(workbench, KernelBacktestPort(factory, data))
    lake = LakePort(data)
    commands = CommandPort(project, backtests, data)
    return Protocol(Modules(backtests, workbench, sources, lake, commands))


class KernelBacktestPort:
    """把固定 Kernel 适配成统一协议中的一个端口。"""

    def __init__(self, factory: RunFactory, data: DataPort) -> None:
        self._kernel = Kernel(factory)
        self._data = data

    def handle(self, operation: str, data: JsonValue) -> PortResult:
        if operation != "workbench.backtest":
            raise BacktestError("unsupported_operation", operation)
        request = _request(data)
        result = self._kernel.run(request)
        value = _result_data(result)
        save_backtest(self._data, request, value)
        return PortResult(
            "resource", "lianghua:backtest-result",
            "urn:lianghua:ui:output:workbench.backtest:v1", value,
        )


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
            return [self._problem(request.request_id, error.code, str(error), error.code == "busy")]
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
    def _problem(correlation_id: str | None, code: str, detail: str, retryable: bool = False) -> ui.UiMessage:
        problem = ui.UiProblem(
            type="urn:lianghua:problem:protocol", code=code,
            title="协议请求未完成", detail=detail, retryable=retryable,
        )
        return ui.UiMessage(
            message_id=uuid4().hex, correlation_id=correlation_id,
            kind="problem", type=problem.type, schema=PROBLEM_SCHEMA, data=problem.to_dict(),
        )


def _request(data: JsonValue) -> BacktestRequest:
    if not isinstance(data, dict):
        raise BacktestError("invalid_request", "backtest data must be an object")
    try:
        return BacktestRequest(
            run_id=_text(data, "run_id") if "run_id" in data else uuid4().hex,
            strategy_id=_text(data, "module"), parameters=_object(data, "params"),
            dataset_id=_text(data, "dataset_id"),
            start=_instant(data, "start"),
            end=_instant(data, "end"),
            initial_cash=Decimal(_text(data, "initial_cash")),
        )
    except BacktestError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BacktestError("invalid_request", str(error)) from error


def _text(data: Mapping[str, JsonValue], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _identity(data: Mapping[str, JsonValue], name: str) -> str:
    value = _text(data, name)
    if Path(value).name != value:
        raise ValueError(f"{name} must be a data identity")
    return value


def _instant(data: Mapping[str, JsonValue], name: str) -> datetime:
    value = datetime.fromisoformat(_text(data, name))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _object(data: Mapping[str, JsonValue], name: str) -> Mapping[str, object]:
    value = data[name]
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _result_data(result: BacktestResponse) -> JsonObject:
    return {
        "run_id": result.run_id, "strategy_id": result.strategy_id, "dataset_id": result.dataset_id,
        "first_at": result.first_at.isoformat(), "last_at": result.last_at.isoformat(),
        "steps": result.steps, "fills": result.fills, "final_cash": str(result.final_cash),
        "final_equity": str(result.final_equity),
        "positions": {symbol: str(quantity) for symbol, quantity in result.positions.items()},
        "pending_orders": result.pending_orders,
    }


__all__ = [
    "BacktestError", "BacktestModule", "CommandPort", "KernelBacktestPort", "Modules", "OperationSpec", "PortResult",
    "Protocol", "PROTOCOL_NAME", "PROTOCOL_VERSION", "export_operations",
    "build_backend", "build_protocol", "build_data", "DataBus", "DataPort", "DataRef", "DataError",
]
