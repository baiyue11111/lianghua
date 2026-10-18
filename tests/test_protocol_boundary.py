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
    def test_frontend_catalog_matches_backend_and_local_collect(self):
        import subprocess
        from backend import export_operations
        root = Path(__file__).resolve().parents[1]
        script = "import {features} from './frontend/display/dist/features.mjs';console.log(JSON.stringify(features().operations.map(x=>x.operation)))"
        output = subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=root, text=True)
        names = json.loads(output)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(export_operations()) | {'cli.collect'})

    def test_collection_port_owns_collector_lifecycle_once(self):
        from backend import CollectionPort, Modules, Protocol
        class Collector:
            data = None
            def __init__(self): self.calls = []
            def start(self): self.calls.append("start")
            def close(self): self.calls.append("close")
        collector = Collector()
        port = CollectionPort(collector)
        backend = Protocol(Modules(Port({}), Port({}), port, Port({}), Port({})))
        backend.start()
        backend.close()
        self.assertEqual(collector.calls, ["start", "close"])

    def test_data_requests_do_not_enter_collection_port(self):
        from backend import Modules, Protocol
        sources, data = Port({}), Port({"ready": True})
        backend = Protocol(Modules(Port({}), Port({}), sources, data, Port({})))
        request = ui.UiRequest("data-1", "data.status", "urn:lianghua:ui:input:data.status:v1", {})
        messages = asyncio.run(backend.send(request))
        self.assertEqual(messages[0].data, {"ready": True})
        self.assertEqual(sources.calls, [])
        self.assertEqual(data.calls, [("data.status", {})])

    def test_retired_operations_are_rejected_without_aliases(self):
        from backend import Modules, Protocol
        ports = [Port({}) for _ in range(5)]
        backend = Protocol(Modules(*ports))
        for name in ("lake.status", "workbench.backtest"):
            request = ui.UiRequest("old-1", name, f"urn:lianghua:ui:input:{name}:v1", {})
            messages = asyncio.run(backend.send(request))
            self.assertEqual(messages[0].data["code"], "unknown_operation")
        self.assertTrue(all(not port.calls for port in ports))

    def test_http_transport_has_one_route_and_calls_only_backend_port(self):
        from frontend.web import create_app

        class Backend:
            def __init__(self):
                self.requests = []

            async def send(self, request):
                self.requests.append(request)
                return [ui.UiMessage(
                    "message-1", "resource", "urn:test:result",
                    "urn:lianghua:ui:output:data.status:v1", {"ok": True},
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
            "request-1", "data.status", "urn:lianghua:ui:input:data.status:v1", {}
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
            "application", "core", "history_data", "lake", "protocol",
            "strategy", "tools",
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
        self.assertEqual(kernel_importers, ["src/backend.py"])

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
        from backend import Modules, Protocol, build_protocol, export_operations

        operations = export_operations()
        self.assertEqual(set(operations), {
            "backtest.strategies", "backtest.prepare", "backtest.run",
            "cli.backtest", "collection.describe", "collection.inspect", "collection.run", "cli.ingest", "research.runs", "research.run", "research.benchmarks",
            "research.benchmark", "data.research_inputs", "collection.sources", "collection.sync",
            "collection.schedule", "collection.job", "collection.snapshot",
            "collection.evidence",
            "data.status", "data.search", "data.bars",
            "collection.start", "collection.current", "collection.stop",
            "collection.automatic", "collection.set_automatic", "collection.merge",
            "collection.merge_job", "data.check", "collection.repair", "collection.repair_job",
            "cli.ingest",
        })
        self.assertEqual(operations["backtest.run"]["mode"], "write")
        self.assertEqual(operations["data.status"]["mode"], "read")
        modules = Modules(*(Port({}) for _ in range(5)))
        self.assertIsInstance(build_protocol(modules), Protocol)

    def test_send_routes_non_backtest_requests_to_the_declared_module_port(self):
        from backend import Modules, Protocol

        value = {"items": ["one"]}
        source = Port(value)
        protocol = Protocol(Modules(
            backtests=Port({}), research=Port({}), sources=source,
            data=Port({}), commands=Port({}),
        ))
        request = ui.UiRequest("request-1", "collection.sources", "urn:lianghua:ui:input:collection.sources:v1", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].correlation_id, "request-1")
        self.assertEqual(messages[0].data, value)
        self.assertEqual(source.calls, [("collection.sources", {})])

    def test_cli_backtest_uses_the_same_backtest_module_without_request_wrapper(self):
        from backend import CommandPort

        backtests = Port({"run_id": "shared"})
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            request = {
                "run_id": "shared",
                "module": "plugins.strategies.moving_average_cross", "params": {},
                "dataset_id": "history",
                "intervals": [{"start": "2020-01-01", "end": "2020-12-31"}],
                "initial_cash": "100000.00",
            }
            path.write_text(json.dumps(request))

            result = CommandPort(Path(root), backtests, __import__('storage').DataStore(Path(root))).handle(
                "cli.backtest", {"config": str(path), "root": str(root)}
            )

        result = result.data
        self.assertEqual(result["run_id"], "shared")
        self.assertEqual(result["intervals"], [{
            "index": 1, "name": None, "run_id": "shared-interval-0001",
            "result": {"run_id": "shared"},
        }])
        self.assertEqual(backtests.calls, [("backtest.run", {
            "run_id": "shared-interval-0001",
            "module": "plugins.strategies.moving_average_cross", "params": {},
            "dataset_id": "history", "start": "2020-01-01", "end": "2020-12-31",
            "initial_cash": "100000.00",
        })])

    def test_cli_intervals_keep_input_order_labels_and_single_range_payloads(self):
        from backend import CommandPort, PortResult

        class Results:
            def __init__(self):
                self.calls = []

            def handle(self, operation, data):
                self.calls.append((operation, data))
                return PortResult("resource", "result", "schema", {
                    "run_id": data["run_id"], "effective_config": {
                        "start": data["start"], "end": data["end"],
                    },
                })

        backtests = Results()
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            path.write_text(json.dumps({
                "run_id": "batch", "module": "plugins.strategies.moving_average_cross",
                "params": {"fast_window": 1}, "dataset_id": "history",
                "initial_cash": "100000", "intervals": [
                    {"name": "甲", "start": "2025-01-01", "end": "2025-01-31"},
                    {"name": "甲", "start": "2024-01-01", "end": "2024-12-31"},
                    {"start": "2024-06-01", "end": "2024-09-01"},
                ],
            }))
            result = CommandPort(Path(root), backtests, __import__("storage").DataStore(Path(root))).handle(
                "cli.backtest", {"config": str(path)}
            )

        result = result.data
        self.assertEqual(result["run_id"], "batch")
        self.assertEqual([item["index"] for item in result["intervals"]], [1, 2, 3])
        self.assertEqual([item["name"] for item in result["intervals"]], ["甲", "甲", None])
        self.assertEqual([call[1]["run_id"] for call in backtests.calls], [
            "batch-interval-0001", "batch-interval-0002", "batch-interval-0003",
        ])
        self.assertEqual([(call[1]["start"], call[1]["end"]) for call in backtests.calls], [
            ("2025-01-01", "2025-01-31"),
            ("2024-01-01", "2024-12-31"),
            ("2024-06-01", "2024-09-01"),
        ])
        self.assertTrue(all("intervals" not in call[1] for call in backtests.calls))

    def test_cli_validates_every_interval_before_calling_backtest_port(self):
        from backend import CommandPort

        backtests = Port({"run_id": "unused"})
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            path.write_text(json.dumps({
                "run_id": "batch", "module": "plugins.strategies.moving_average_cross",
                "params": {}, "dataset_id": "history", "initial_cash": "100000",
                "intervals": [
                    {"start": "2020-01-01", "end": "2020-01-02"},
                    {"name": "乙", "start": "bad-date", "end": "2020-01-03"},
                ],
            }))
            with self.assertRaises(ValueError) as caught:
                CommandPort(Path(root), backtests, __import__("storage").DataStore(Path(root))).handle(
                    "cli.backtest", {"config": str(path)}
                )

        self.assertIn("interval 2", str(caught.exception))
        self.assertIn("乙", str(caught.exception))
        self.assertIn("batch-interval-0002", str(caught.exception))
        self.assertEqual(backtests.calls, [])

    def test_cli_rejects_legacy_ranges_and_search_without_running(self):
        from backend import CommandPort

        backtests = Port({"run_id": "unused"})
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            base = {
                "run_id": "batch", "module": "plugins.strategies.moving_average_cross",
                "params": {}, "dataset_id": "history", "initial_cash": "100000",
                "intervals": [{"start": "2020-01-01", "end": "2020-01-02"}],
            }
            command = CommandPort(Path(root), backtests, __import__("storage").DataStore(Path(root)))
            for extra in (
                {"start": "2020-01-01", "end": "2020-01-02"},
                {"validation": {"start": "2020-01-03", "end": "2020-01-04"}},
                {"out_of_sample": {"start": "2020-01-03", "end": "2020-01-04"}},
                {"search": {}},
            ):
                with self.subTest(extra=extra):
                    path.write_text(json.dumps({**base, **extra}))
                    with self.assertRaises(ValueError):
                        command.handle("cli.backtest", {"config": str(path)})

        self.assertEqual(backtests.calls, [])

    def test_cli_prevalidates_batch_and_interval_run_ids_with_data_path(self):
        from backend import CommandPort

        backtests = Port({"run_id": "unused"})
        with TemporaryDirectory() as root:
            path = Path(root) / "run.json"
            base = {
                "module": "plugins.strategies.moving_average_cross", "params": {},
                "dataset_id": "history", "initial_cash": "100000",
                "intervals": [{"start": "2020-01-01", "end": "2020-01-02"}],
            }
            command = CommandPort(Path(root), backtests, __import__("storage").DataStore(Path(root)))
            for run_id in ("../escape", ".hidden", "x" * 300):
                with self.subTest(run_id=run_id):
                    path.write_text(json.dumps({**base, "run_id": run_id}))
                    with self.assertRaises((ValueError, OSError)):
                        command.handle("cli.backtest", {"config": str(path)})

        self.assertEqual(backtests.calls, [])

    def test_cli_stops_after_failure_and_saves_completed_results_with_failure_context(self):
        from protocol import BacktestError
        from backend import CommandPort, PortResult
        from storage import DataStore

        class FailingPort:
            def __init__(self):
                self.calls = []

            def handle(self, operation, data):
                self.calls.append(data)
                if data["run_id"].endswith("0002"):
                    raise BacktestError("run_failed", "fixture failure")
                return PortResult("resource", "result", "schema", {"run_id": data["run_id"]})

        backtests = FailingPort()
        with TemporaryDirectory() as root:
            root = Path(root)
            path = root / "run.json"
            path.write_text(json.dumps({
                "run_id": "batch-failure", "module": "plugins.strategies.moving_average_cross",
                "params": {}, "dataset_id": "history", "initial_cash": "100000",
                "intervals": [
                    {"name": "甲", "start": "2024-01-01", "end": "2024-01-31"},
                    {"name": "乙", "start": "2024-02-01", "end": "2024-02-29"},
                    {"name": "丙", "start": "2025-01-01", "end": "2025-01-31"},
                ],
            }))
            data = DataStore(root)
            with self.assertRaises(BacktestError) as caught:
                CommandPort(root, backtests, data).handle("cli.backtest", {"config": str(path)})

            saved = json.loads(data.read("runs", "batch-failure", "failed.json"))

        self.assertEqual(len(backtests.calls), 2)
        self.assertEqual(caught.exception.code, "interval_failed")
        self.assertIn("interval 2", str(caught.exception))
        self.assertIn("乙", str(caught.exception))
        self.assertIn("batch-failure-interval-0002", str(caught.exception))
        self.assertEqual(saved["partial"]["failed"], {
            "index": 2, "name": "乙", "run_id": "batch-failure-interval-0002",
        })
        self.assertEqual(saved["partial"]["completed"][0]["result"], {
            "run_id": "batch-failure-interval-0001",
        })

    def test_cli_busy_preserves_retryable_error_and_does_not_save_failed_record(self):
        from protocol import BacktestError
        from backend import CommandPort
        from storage import DataStore

        class BusyPort:
            def __init__(self):
                self.calls = []

            def handle(self, operation, data):
                self.calls.append(data)
                raise BacktestError("busy", "kernel already has an active run")

        backtests = BusyPort()
        with TemporaryDirectory() as root:
            root = Path(root)
            path = root / "run.json"
            path.write_text(json.dumps({
                "run_id": "batch-busy", "module": "plugins.strategies.moving_average_cross",
                "params": {}, "dataset_id": "history", "initial_cash": "100000",
                "intervals": [
                    {"name": "甲", "start": "2024-01-01", "end": "2024-01-31"},
                    {"name": "乙", "start": "2024-02-01", "end": "2024-02-29"},
                ],
            }))
            data = DataStore(root)
            with self.assertRaises(BacktestError) as caught:
                CommandPort(root, backtests, data).handle("cli.backtest", {"config": str(path)})

            failed_path = data.path("runs", "batch-busy", "failed.json")

        self.assertEqual(caught.exception.code, "busy")
        self.assertIn("interval 1", str(caught.exception))
        self.assertIn("甲", str(caught.exception))
        self.assertIn("batch-busy-interval-0001", str(caught.exception))
        self.assertEqual(len(backtests.calls), 1)
        self.assertFalse(failed_path.exists())

    def test_backtest_port_is_the_only_protocol_owned_kernel_adapter(self):
        from backend import KernelBacktestPort, Modules, Protocol
        from market import DataBus
        from storage import DataStore
        from tests.history_fixture import make_snapshot
        from tests.fee_fixture import configured_settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            protocol = Protocol(Modules(
                backtests=KernelBacktestPort(bus, configured_settlement()),
                research=Port({}), sources=Port({}), data=Port({}), commands=Port({}),
            ))
            request = ui.UiRequest(
                "request-2", "backtest.run", "urn:lianghua:ui:input:backtest.run:v1",
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
        from backend import Modules, Protocol

        ports = [Port({}) for _ in range(5)]
        protocol = Protocol(Modules(*ports))
        request = ui.UiRequest("request-3", "unknown.operation", "urn:unknown", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(messages[0].kind, "problem")
        self.assertEqual(messages[0].data["code"], "unknown_operation")
        self.assertTrue(all(not port.calls for port in ports))

    def test_module_failure_is_a_protocol_problem(self):
        from backend import Modules, Protocol

        class Broken(Port):
            def handle(self, operation, data):
                raise RuntimeError("storage is unavailable")

        protocol = Protocol(Modules(
            backtests=Port({}), research=Port({}), sources=Broken({}),
            data=Port({}), commands=Port({}),
        ))
        request = ui.UiRequest("request-4", "collection.sources", "urn:lianghua:ui:input:collection.sources:v1", {})

        messages = asyncio.run(protocol.send(request))

        self.assertEqual(messages[0].kind, "problem")
        self.assertEqual(messages[0].data["code"], "module_failed")
        self.assertEqual(messages[0].schema, "urn:lianghua:ui:problem:v1")
