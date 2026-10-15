"""前端内核唯一的、与业务无关的消息协议。

后端只产生 :class:`UiMessage`，``frontend/display`` 只消费它。
CLI、Web、React 和 HTTP、WebSocket、SSE 都是协议外的适配器。

线格式是 JSON。这个 Python 文件提供 Python 侧的不可变模型和序列化方法，
本文件是唯一手工维护的公共契约源；JSON Schema 和 TypeScript 类型从此生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from math import isfinite
from typing import Annotated, Awaitable, Callable, Literal, Mapping, Protocol, TypeAlias, get_args, get_origin, get_type_hints
import inspect
import collections.abc
import json
import asyncio
import subprocess
import selectors
import time
from pathlib import Path

from pydantic import ConfigDict, Field, TypeAdapter


PROTOCOL_NAME = "lianghua.ui"
PROTOCOL_VERSION = 1

MessageKind: TypeAlias = Literal[
    "resource",
    "collection",
    "series",
    "document",
    "operation",
    "event",
    "problem",
    "custom",
]

JsonScalar: TypeAlias = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
Text = Annotated[str, Field(min_length=1)]
Extension = Annotated[str, Field(pattern=r"^(.*:.*|x-.*)$")]


class _Wire:
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
JsonObject: TypeAlias = dict[str, JsonValue]

_KINDS = frozenset(
    {
        "resource",
        "collection",
        "series",
        "document",
        "operation",
        "event",
        "problem",
        "custom",
    }
)


def _check_json(value: object, name: str) -> None:
    """尽早拒绝不能放进线协议的值。"""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{name} must contain JSON values")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{name} object keys must be strings")
            _check_json(item, f"{name}.{key}")
        return
    raise TypeError(f"{name} must contain JSON values")


def _check_text(value: str, name: str) -> None:
    if not value:
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class FieldHint(_Wire):
    """给显示层的字段语义提示。

    这里描述数据的含义，不描述 React 组件、HTML、CSS 或布局。
    """

    path: Text
    label: str | None = None
    role: str | None = None
    format: str | None = None
    unit: str | None = None
    precision: int | None = None
    order: int | None = None
    visible: bool | None = None

    def __post_init__(self) -> None:
        _check_text(self.path, "field path")

    def to_dict(self) -> JsonObject:
        value: JsonObject = {"path": self.path}
        for name in (
            "label",
            "role",
            "format",
            "unit",
            "precision",
            "order",
            "visible",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = item
        return value


@dataclass(frozen=True)
class Presentation(_Wire):
    """可选的语义呈现提示。"""

    title: str | None = None
    role: str | None = None
    fields: tuple[FieldHint, ...] = ()

    def to_dict(self) -> JsonObject:
        value: JsonObject = {}
        if self.title is not None:
            value["title"] = self.title
        if self.role is not None:
            value["role"] = self.role
        if self.fields:
            value["fields"] = [item.to_dict() for item in self.fields]
        return value


@dataclass(frozen=True)
class UiLink(_Wire):
    """消息中的关系链接，不规定具体传输方式。"""

    rel: Text
    href: Text
    type: str | None = None

    def __post_init__(self) -> None:
        _check_text(self.rel, "link rel")
        _check_text(self.href, "link href")

    def to_dict(self) -> JsonObject:
        value: JsonObject = {"rel": self.rel, "href": self.href}
        if self.type is not None:
            value["type"] = self.type
        return value


@dataclass(frozen=True)
class UiProblem(_Wire):
    """可放入 ``problem`` 消息正文的通用问题。"""

    type: Text
    code: Text
    title: Text
    detail: str | None = None
    field: str | None = None
    retryable: bool = False
    extensions: Mapping[Extension, JsonValue] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_text(self.type, "problem type")
        _check_text(self.code, "problem code")
        _check_text(self.title, "problem title")
        _check_extensions(self.extensions)

    def to_dict(self) -> JsonObject:
        value: JsonObject = {
            "type": self.type,
            "code": self.code,
            "title": self.title,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            value["detail"] = self.detail
        if self.field is not None:
            value["field"] = self.field
        if self.extensions:
            value["extensions"] = dict(self.extensions)
        return value


def _check_extensions(extensions: Mapping[Extension, JsonValue]) -> None:
    for name, value in extensions.items():
        if not isinstance(name, str):
            raise TypeError("extension names must be strings")
        if ":" not in name and not name.startswith("x-"):
            raise ValueError("extension names must be namespaced")
        _check_text(name, "extension name")
        _check_json(value, f"extension {name}")


@dataclass(frozen=True)
class UiMessage(_Wire):
    """后端和前端之间唯一的通用语义消息。"""

    message_id: Text
    kind: MessageKind
    type: Text
    schema: Text
    data: JsonValue
    correlation_id: str | None = None
    presentation: Presentation | None = None
    links: tuple[UiLink, ...] = ()
    extensions: Mapping[Extension, JsonValue] = dataclass_field(default_factory=dict)
    protocol: Literal["lianghua.ui"] = PROTOCOL_NAME
    version: Literal[1] = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _check_text(self.message_id, "message id")
        _check_text(self.type, "message type")
        _check_text(self.schema, "message schema")
        if self.protocol != PROTOCOL_NAME:
            raise ValueError(f"unsupported protocol: {self.protocol}")
        if self.version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version: {self.version}")
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported message kind: {self.kind}")
        _check_json(self.data, "message data")
        _check_extensions(self.extensions)

    def to_dict(self) -> JsonObject:
        value: JsonObject = {
            "protocol": self.protocol,
            "version": self.version,
            "message_id": self.message_id,
            "kind": self.kind,
            "type": self.type,
            "schema": self.schema,
            "data": self.data,
        }
        if self.correlation_id is not None:
            value["correlation_id"] = self.correlation_id
        if self.presentation is not None:
            value["presentation"] = self.presentation.to_dict()
        if self.links:
            value["links"] = [link.to_dict() for link in self.links]
        if self.extensions:
            value["extensions"] = dict(self.extensions)
        return value


class UiProtocol(Protocol):
    """显示内核的唯一接收端口。

    传输适配器只负责把外部输入变成 ``UiMessage``，实现者负责消费消息。
    这个端口不暴露 HTTP、CLI、React 或任何后端领域类型。
    """

    def receive(self, message: UiMessage) -> None: ...


ActionKind = Literal["edit", "select", "query", "invoke", "reset", "dismiss"]


@dataclass(frozen=True)
class UiAction(_Wire):
    action_id: Text
    kind: ActionKind
    target: Text
    data: JsonValue = None
    operation: Text | None = None

    def __post_init__(self) -> None:
        _check_text(self.action_id, "action id")
        _check_text(self.target, "target")
        if self.kind not in {"edit", "select", "query", "invoke", "reset", "dismiss"}:
            raise ValueError("unsupported action kind")
        if self.kind == "invoke" and not self.operation:
            raise ValueError("invoke requires operation")
        _check_json(self.data, "action data")

    def to_dict(self) -> JsonObject:
        value = {"action_id": self.action_id, "kind": self.kind, "target": self.target, "data": self.data}
        if self.operation is not None:
            value["operation"] = self.operation
        return value


@dataclass(frozen=True)
class UiRequest(_Wire):
    request_id: Text
    operation: Text
    schema: Text
    data: JsonValue

    def to_dict(self) -> JsonObject:
        return {"request_id": self.request_id, "operation": self.operation, "schema": self.schema, "data": self.data}


@dataclass(frozen=True)
class UiOperation(_Wire):
    operation: Text
    input_schema: Text
    response_schemas: tuple[Text, ...]
    mode: Literal["read", "write"] = "read"


@dataclass(frozen=True)
class UiOperationState(_Wire):
    available: bool = False
    status: Literal["idle", "pending", "succeeded", "failed"] = "idle"
    request_id: str | None = None
    revision: int = 0
    input: JsonValue = None
    result_input: JsonValue = None


@dataclass(frozen=True)
class UiSlot(_Wire):
    revision: int = 0
    values: JsonObject = dataclass_field(default_factory=dict)
    selection: JsonValue = None
    query: JsonObject = dataclass_field(default_factory=dict)
    messages: tuple[UiMessage, ...] = ()
    problems: tuple[UiProblem, ...] = ()
    operations: Mapping[str, UiOperationState] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class UiState(_Wire):
    revision: int = 0
    targets: Mapping[str, UiSlot] = dataclass_field(default_factory=dict)


class FrontendPort(Protocol):
    def dispatch(self, action: UiAction) -> Awaitable[UiState]: ...
    def snapshot(self) -> UiState: ...
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class BackendPort(Protocol):
    def send(self, request: UiRequest) -> Awaitable[list[UiMessage]]: ...


# Bridge frames are transport data, not another public business protocol.
@dataclass(frozen=True)
class UiFrame(_Wire):
    kind: Literal["action", "request", "response", "state", "close"]
    action: UiAction | None = None
    request: UiRequest | None = None
    messages: tuple[UiMessage, ...] = ()
    state: UiState | None = None


_MODELS = {model.__name__: model for model in (
    UiAction, UiRequest, UiState, UiMessage, UiProblem, UiOperation, UiFrame,
)}


def parse_wire(name: str, value: object):
    return TypeAdapter(_MODELS[name]).validate_json(json.dumps(value, allow_nan=False))


def exchange(action: UiAction) -> UiState:
    """运行共用交互核心，并在此处连接唯一 backend protocol。"""
    core = Path(__file__).parent / "display" / "runtime" / "cli.mjs"
    if not core.is_file():
        raise OSError("交互核心发布产物缺失；请运行 npm run build --prefix frontend/display")
    from protocal import build_backend
    backend = build_backend(Path.cwd())
    process = None
    try:
        process = subprocess.Popen(
            ["node", str(core), "--connected"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1,
        )
        def write(frame: object) -> None:
            parse_wire("UiFrame", frame)
            if process.stdin is None:
                raise RuntimeError("交互核心输入不可用")
            process.stdin.write(json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n")
            process.stdin.flush()

        write({"kind": "action", "action": action.to_dict()})
        deadline = time.monotonic() + 30
        if process.stdout is None:
            raise RuntimeError("交互核心输出不可用")
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while True:
                if not ready.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("交互核心响应超时")
                line = process.stdout.readline()
                if not line:
                    detail = process.stderr.read().strip() if process.stderr else ""
                    raise RuntimeError(detail or "交互核心提前结束")
                frame = parse_wire("UiFrame", json.loads(line))
                if frame.kind == "state" and frame.state is not None:
                    write({"kind": "close"})
                    return frame.state
                if frame.kind != "request" or frame.request is None:
                    raise ValueError("无效桥接消息")
                try:
                    messages = asyncio.run(backend.send(frame.request))
                except Exception as exc:
                    issue = UiProblem("urn:lianghua:ui:problem", "backend_failed", str(exc))
                    messages = [UiMessage(
                        "failure", "problem", "urn:lianghua:ui:problem",
                        "urn:lianghua:ui:problem:v1", issue.to_dict(),
                        correlation_id=frame.request.request_id,
                    )]
                write({"kind": "response", "request": frame.request.to_dict(),
                       "messages": [message.to_dict() for message in messages]})
                deadline = time.monotonic() + 30
    finally:
        try:
            backend.close()
        finally:
            if process is not None and process.stdin:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                if process is not None:
                    _close_process(process)
            finally:
                if process is not None and process.stdout:
                    process.stdout.close()
                if process is not None and process.stderr:
                    process.stderr.close()


def _close_process(process) -> None:
    """Reap the shared core, escalating when it ignores a graceful stop."""
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("交互核心进程无法回收") from error


def export_schema() -> dict:
    schema = TypeAdapter(UiAction | UiRequest | UiState | UiMessage | UiProblem | UiOperation | UiFrame).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:lianghua:ui:v1"
    schema["$defs"]["UiAction"]["allOf"] = [{
        "if": {"properties": {"kind": {"const": "invoke"}}, "required": ["kind"]},
        "then": {"required": ["operation"], "properties": {"operation": {"type": "string", "minLength": 1}}},
    }]
    for name in ("UiMessage", "UiProblem"):
        schema["$defs"][name]["properties"]["extensions"]["additionalProperties"] = False
    return schema


def export_ports() -> str:
    """Derive TypeScript port signatures from the Python public interfaces."""
    def type_name(value):
        if value is type(None):
            return "void"
        origin, args = get_origin(value), get_args(value)
        if origin is collections.abc.Awaitable:
            return "Promise<" + type_name(args[0]) + ">"
        if origin is list:
            return type_name(args[0]) + "[]"
        if origin is collections.abc.Callable:
            params = ", ".join(f"arg{i}: {type_name(item)}" for i, item in enumerate(args[0]))
            return f"({params}) => {type_name(args[1])}"
        return value.__name__

    definitions = []
    for port in (FrontendPort, BackendPort, UiProtocol):
        methods = []
        for name, method in vars(port).items():
            if name.startswith("_") or not callable(method):
                continue
            hints = get_type_hints(method)
            params = ", ".join(f"{param}: {type_name(hints[param])}"
                for param in inspect.signature(method).parameters if param != "self")
            methods.append(f"  {name}({params}): {type_name(hints['return'])};")
        definitions.append("export interface " + port.__name__ + " {\n" + "\n".join(methods) + "\n}")
    return "\n\n".join(definitions) + "\n"
