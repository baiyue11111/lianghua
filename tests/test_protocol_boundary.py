import asyncio
import ast
import importlib
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import asdict


ui = importlib.import_module("frontend.ui-protocol")


class Port:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def handle(self, operation, data):
        self.calls.append((operation, data))
        return self.value


class ProtocolBoundaryTests(unittest.TestCase):
    def test_http_transport_has_one_route_and_calls_only_backend_port(self):
        from frontend.web import create_app

        class Backend:
            def __init__(self):
                self.requests = []

            async def send(self, request):
                self.requests.append(request)
                return [ui.UiMessage(
                    "message-1", "resource", "urn:test:result",
                    "urn:lianghua:ui:output:lake.status:v1", {"ok": True},
                    correlation_id=request.request_id,
                )]

        async def call(app, payload):
            body = json.dumps(payload).encode()
            sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(value):
                sent.append(value)

            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "method": "POST",
                "path": "/api/protocol", "raw_path": b"/api/protocol",
                "query_string": b"", "headers": [(b"content-type", b"application/json")],
                "scheme": "http", "server": ("test", 80), "client": ("test", 1),
                "root_path": "",
            }
            await app(scope, receive, send)
            status = next(item["status"] for item in sent if item["type"] == "http.response.start")
            raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            return status, json.loads(raw)

        backend = Backend()
        app = create_app(backend)
        request = ui.UiRequest(
            "request-1", "lake.status", "urn:lianghua:ui:input:lake.status:v1", {}
        )
        status, body = asyncio.run(call(app, request.to_dict()))

        self.assertEqual(status, 200)
        self.assertEqual(body[0]["correlation_id"], "request-1")
        self.assertEqual(backend.requests, [request])
        self.assertEqual(
            {route.path for route in app.routes if route.path.startswith("/api/")},
            {"/api/protocol"},
        )

    def test_frontend_adapters_do_not_import_business_modules(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = {
            "application", "core", "history_data", "lake", "protocal",
            "research_registry", "strategy", "tools",
        }
        for relative in ("frontend/web.py", "frontend/cli.py"):
            path = root / relative
            imports = []
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    imports.append((node.module or "").split(".")[0])
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
            self.assertFalse(forbidden.intersection(imports), (relative, imports))

    def test_kernel_and_browser_have_one_protocol_connection(self):
        root = Path(__file__).resolve().parents[1]
        kernel_importers = []
        for path in (root / "src").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module == "core.kernel":
                    kernel_importers.append(path.relative_to(root).as_posix())
                elif isinstance(node, ast.Import) and any(alias.name == "core.kernel" for alias in node.names):
                    kernel_importers.append(path.relative_to(root).as_posix())
        self.assertEqual(kernel_importers, ["src/protocal.py"])

        source = (root / "frontend/web/src/main.tsx").read_text()
        self.assertIn("createBackend", source)
        self.assertIn("<App backend={backend}", source)

    def test_frontend_has_no_backend_api_package_or_business_routes(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse(list((root / "frontend" / "api").glob("*.py")))
        routes = []
        for path in (root / "frontend").rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if (isinstance(decorator, ast.Call)
                            and isinstance(decorator.func, ast.Attribute)
                            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
                            and decorator.args
                            and isinstance(decorator.args[0], ast.Constant)
                            and str(decorator.args[0].value).startswith("/api/")):
                        routes.append((path.relative_to(root).as_posix(), decorator.args[0].value))
        self.assertEqual(routes, [("frontend/web.py", "/api/protocol")])

    def test_operation_directory_is_the_single_public_operation_list(self):
        from protocal import Modules, Protocol, build_protocol, export_operations

        operations = export_operations()
        self.assertEqual(set(operations), {
            "workbench.strategies", "workbench.preflight", "workbench.backtest",
            "cli.run", "workbench.runs", "workbench.run", "workbench.benchmarks",
            "workbench.benchmark", "lake.researchDaily", "lake.researchInputs",
            "lake.closedLoop", "workbench.sources", "workbench.sync",
            "workbench.schedule", "workbench.job", "workbench.snapshot",
            "workbench.evidence", "workbench.failedEvidence", "workbench.reviews",
            "workbench.review", "lake.status", "lake.query", "lake.kline",
            "lake.startFetch", "lake.fetchJob", "lake.stopFetch",
            "lake.continuousFetch", "lake.setContinuousFetch", "lake.startMerge",
            "lake.mergeJob", "lake.doctor", "lake.doctorFix", "lake.doctorJob",
            "cli.ingest", "cli.report",
        })
        self.assertEqual(operations["workbench.backtest"]["mode"], "write")
        self.assertEqual(operations["lake.status"]["mode"], "read")
        modules = Modules(*(Port({}) for _ in range(5)))
        self.assertIsInstance(build_protocol(modules), Protocol)

    def test_send_routes_non_backtest_requests_to_the_declared_module_port(self):
        from protocal import Modules, Protocol

        value = {"items": ["one"]}
        source = Port(value)
        protocol = Protocol(Modules(
            backtests=Port({}), research=Port({}), sources=source,
            lake=Port({}), commands=Port({}),
        ))
        request = ui.UiRequest("request-1", "workbench.sources", "urn:lianghua:ui:input:workbench.sources:v1", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].correlation_id, "request-1")
        self.assertEqual(messages[0].data, value)
        self.assertEqual(source.calls, [("workbench.sources", {})])

    def test_cli_run_uses_the_same_backtest_module(self):
        from protocal import CommandPort

        backtests = Port({"run_id": "shared"})
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            request = {
                "module": "plugins.strategies.moving_average_cross", "params": {},
                "dataset_id": "history", "start": "2020-01-01", "end": "2020-12-31",
                "initial_cash": "100000.00",
            }
            path.write_text(json.dumps({"request": request}))

            result = CommandPort(Path(root), backtests, __import__('storage').DataStore(Path(root))).handle(
                "cli.run", {"config": str(path), "root": root}
            )

        self.assertEqual(result, {"run_id": "shared"})
        self.assertEqual(backtests.calls, [("workbench.backtest", request)])

    def test_backtest_port_is_the_only_protocol_owned_kernel_adapter(self):
        from history_data import HistoricalLake
        from plugins.data import Settlement
        from protocal import KernelBacktestPort, Modules, Protocol
        from protocal import DataBus
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            bus.configure_reader(HistoricalLake(bus, config))
            protocol = Protocol(Modules(
                backtests=KernelBacktestPort(bus, Settlement),
                research=Port({}), sources=Port({}), lake=Port({}), commands=Port({}),
            ))
            request = ui.UiRequest(
                "request-2", "workbench.backtest", "urn:lianghua:ui:input:workbench.backtest:v1",
                {"run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                 "params": {"positions": 4}, "dataset_id": "fixture",
                 "start": "2020-01-31", "end": "2020-02-04",
                 "initial_cash": "100000"},
            )

            messages = asyncio.run(protocol.send(request))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "resource")
        self.assertEqual(messages[0].correlation_id, "request-2")
        self.assertEqual(messages[0].data["run_id"], "one")

    def test_unknown_operation_is_a_problem_without_calling_any_module(self):
        from protocal import Modules, Protocol

        ports = [Port({}) for _ in range(5)]
        protocol = Protocol(Modules(*ports))
        request = ui.UiRequest("request-3", "unknown.operation", "urn:unknown", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(messages[0].kind, "problem")
        self.assertEqual(messages[0].data["code"], "unknown_operation")
        self.assertTrue(all(not port.calls for port in ports))

    def test_module_failure_is_a_protocol_problem(self):
        from protocal import Modules, Protocol

        class Broken(Port):
            def handle(self, operation, data):
                raise RuntimeError("storage is unavailable")

        protocol = Protocol(Modules(
            backtests=Port({}), research=Port({}), sources=Broken({}),
            lake=Port({}), commands=Port({}),
        ))
        request = ui.UiRequest("request-4", "workbench.sources", "urn:lianghua:ui:input:workbench.sources:v1", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(messages[0].kind, "problem")
        self.assertEqual(messages[0].data["code"], "module_failed")
        self.assertEqual(messages[0].schema, "urn:lianghua:ui:problem:v1")
