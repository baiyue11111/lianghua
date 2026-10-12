"""Single HTTP transport for the public UI protocol.

Business operations are not routes.  The transport accepts one ``UiRequest``
and forwards it to the injected ``BackendPort``.
"""

from __future__ import annotations

from importlib import import_module

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


ui = import_module("frontend.ui-protocol")


def create_app(backend: ui.BackendPort) -> FastAPI:
    app = FastAPI(title="Lianghua Protocol Transport")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST"],
        allow_headers=["content-type"],
    )

    @app.post("/api/protocol")
    async def exchange(value: dict) -> list[dict]:
        request = ui.parse_wire("UiRequest", value)
        messages = await backend.send(request)
        return [message.to_dict() for message in messages]

    return app


__all__ = ["create_app"]
