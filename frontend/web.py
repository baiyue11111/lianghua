"""Single HTTP transport for the public UI protocol.

Business operations are not routes.  The transport accepts one ``UiRequest``
and forwards it to the injected ``BackendPort``.

Loopback is not a trust boundary.  Any page a person happens to have open can
POST to ``127.0.0.1``, and the browser will do it as a *simple request* — no
preflight, so no CORS decision is ever asked for — as long as the content type
is ``text/plain``.  Validating ``Host`` alone therefore guards the wrong door:
every one of those requests carries ``Host: 127.0.0.1:8000`` because that is
where it is going.  Four checks hold the boundary instead:

* ``Host`` must name the loopback interface.  This is what stops DNS rebinding,
  where the attacker keeps its own hostname and moves the address to loopback.
* an ``Origin``, when present, must be a loopback origin.  A cross-site request
  always carries one, so this is what stops the simple-request drive-by.
* the body must be declared ``application/json``.  Anything else is a media
  type browsers are willing to send without asking, so it is refused — which is
  also why the UI never needs a CORS grant.
* the body is read with a ceiling, so a page cannot push an unbounded payload
  into a local process.

Two more rules keep this adapter as strict as the protocol behind it: a request
that cannot be parsed still comes back as a ``problem`` message, so a client
never has to tell "bad request" from "backend broken" by reading a bare status
code; and every refusal names the rule it enforced.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from importlib import import_module
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


ui = import_module("frontend.ui-protocol")

PROBLEM_SCHEMA = "urn:lianghua:ui:problem:v1"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MEDIA_TYPE = "application/json"
BODY_LIMIT = 1 << 20          # one megabyte; a UiRequest is a few kilobytes
BUDGET = 20.0                 # seconds a UI operation may hold the caller
TITLE = "协议请求未完成"


def _problem(detail: str, code: str = "invalid_request", retryable: bool = False) -> dict:
    """Build the same envelope the protocol returns, so clients parse one shape."""

    problem = ui.UiProblem(
        type="urn:lianghua:problem:protocol",
        code=code, title=TITLE, detail=detail[:400],
        retryable=retryable, extensions={},
    )
    message = ui.UiMessage(
        message_id=uuid4().hex, kind="problem", type=problem.type,
        schema=PROBLEM_SCHEMA, data=problem.to_dict(), correlation_id=None,
        presentation=ui.Presentation(title=TITLE, role="problem"),
        extensions={"lianghua:code": code},
    )
    return message.to_dict()


def _reject(status: int, detail: str, code: str = "invalid_request",
            retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content=[_problem(detail, code, retryable)])


def _host_name(value: str) -> str:
    """Reduce a Host or authoring value to its bare host name."""

    value = value.strip().lower()
    if value.startswith('[') and ']' in value:
        return value[1:value.index(']')]          # [::1]:8000 -> ::1
    if value.count(':') == 1:
        return value.rsplit(':', 1)[0]            # 127.0.0.1:8000 -> 127.0.0.1
    return value                                  # bare ::1


def _loopback(host: str) -> bool:
    return _host_name(host) in LOOPBACK


def _media_type(raw: str) -> str:
    return raw.split(';', 1)[0].strip().lower()


def _origin_ok(raw: str) -> bool:
    """A present Origin must come from the loopback interface."""

    if raw.strip().lower() == 'null':
        return False                              # sandboxed frame / file://
    try:
        url = urlsplit(raw.strip())
    except ValueError:
        return False
    if url.scheme not in {'http', 'https'} or url.hostname is None:
        return False
    return url.hostname.lower() in LOOPBACK


async def _body(request: Request, limit: int) -> bytes:
    """Read the body, refusing to buffer more than ``limit`` bytes.

    The declared length is a hint, not a promise, so the counting happens while
    reading rather than in a header check that a client can lie about.
    """

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _TooLarge(total)
        chunks.append(chunk)
    return b''.join(chunks)


class _TooLarge(Exception):
    pass


def create_app(backend: ui.BackendPort) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        try:
            backend.start()
            yield
        finally:
            backend.close()

    app = FastAPI(title="Lianghua Protocol Transport", lifespan=lifespan)

    @app.post("/api/protocol")
    async def exchange(request: Request):
        host = request.headers.get("host", "")
        if not host:
            return _reject(403, "transport requires a loopback Host header")
        if not _loopback(host):
            return _reject(403, f"transport serves loopback hosts only: {host}")
        origin = request.headers.get("origin", "")
        if origin and not _origin_ok(origin):
            return _reject(403, f"transport serves loopback origins only: {origin}")
        declared = _media_type(request.headers.get("content-type", ""))
        if declared != MEDIA_TYPE:
            # Refusing this is what keeps the browser from sending the request
            # at all: only a preflighted request may carry application/json.
            return _reject(415, f"request body must be declared {MEDIA_TYPE}, not {declared or 'nothing'}")
        try:
            raw = await _body(request, BODY_LIMIT)
        except _TooLarge:
            return _reject(413, f"request body exceeds {BODY_LIMIT} bytes")
        try:
            value = json.loads(raw)
        except Exception:
            return _reject(400, "request body must be JSON")
        try:
            message = ui.parse_wire("UiRequest", value)
        except Exception as error:
            return _reject(400, f"request is not a UiRequest: {error}")
        try:
            messages = await _run(backend, message)
        except _OverBudget as error:
            return _reject(504, f"{error.operation} exceeded the {BUDGET:.0f}s budget",
                           code="timeout", retryable=True)
        return [item.to_dict() for item in messages]

    return app


async def _run(backend: ui.BackendPort, message):
    """Run one operation under a deadline.

    The backend work happens in a thread, and a thread cannot be cancelled, so
    this bounds how long the *caller* waits rather than how long the work runs.
    Without a bound, one expensive operation holds the request forever: the
    browser gives up, the query keeps going, and every later request queues
    behind whatever the store is still busy with.
    """

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(lambda: asyncio.run(backend.send(message))), BUDGET)
    except TimeoutError:
        raise _OverBudget(message.operation) from None


class _OverBudget(Exception):
    def __init__(self, operation: str) -> None:
        super().__init__(operation)
        self.operation = operation


__all__ = ["create_app"]
