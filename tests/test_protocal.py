import asyncio
import importlib
import json
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from storage import DataStore
from unittest.mock import patch
from tempfile import TemporaryDirectory
from datetime import timedelta

from tests.test_core_backtest import Factory, request
from tests.test_closed_loop_cn_equity import frames, START


ui = importlib.import_module("frontend.ui-protocol")


def send(protocol, request):
    return asyncio.run(protocol.send(request))[0]


def wire_request(value=None, request_id="one"):
    value = value or request(request_id)
    return ui.UiRequest(
        request_id,
        "workbench.backtest",
        "urn:lianghua:ui:input:workbench.backtest:v1",
        {
            "module": value.strategy_id,
            "params": dict(value.parameters),
            "dataset_id": value.dataset_id,
            "start": value.start.isoformat(),
            "end": value.end.isoformat(),
            "initial_cash": str(value.initial_cash),
        },
    )


class ProtocolTests(unittest.TestCase):
    def test_command_outputs_are_resolved_by_the_shared_data_store(self):
        from protocal import CommandPort

        with TemporaryDirectory() as root:
            data = DataStore(Path(root))
            with patch("data.ingest.ingest_exploratory") as ingest:
                ingest.return_value = type("Result", (), {"__dataclass_fields__": {}})()
                CommandPort(Path(root), EmptyPort(), data).handle("cli.ingest", {
                    "price": "price.xlsx", "fundamentals": "fund.xlsx",
                    "valuations": "value.xlsx", "output": "exploratory",
                    "max_symbols": None, "price_part": None,
                })
            self.assertEqual(
                ingest.call_args.kwargs["output_dir"],
                data.path("sources", "exploratory", "manifest.json").parent,
            )
            with self.assertRaises(ValueError):
                CommandPort(Path(root), EmptyPort(), data).handle("cli.ingest", {
                    "price": "p", "fundamentals": "f", "valuations": "v",
                    "output": "../outside", "max_symbols": None, "price_part": None,
                })
            with patch("protocal.subprocess.run") as run:
                result = CommandPort(Path(root), EmptyPort(), data).handle("cli.report", {
                    "artifact": "artifact.json", "output": "report-one",
                    "builder": "build.mjs", "ready_timeout_ms": 1, "timeout_ms": 2,
                })
            expected = data.path("runs", "report-one", "report.html")
            self.assertIn(str(expected), run.call_args.args[0])
            self.assertEqual(result, {"output": str(expected)})

    def test_kernel_result_is_saved_once_in_the_shared_data_store(self):
        from protocal import KernelBacktestPort
        from storage import DataStore
        from workbench.research import Research

        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            payload = {**wire_request().data, "run_id": "one"}
            result = KernelBacktestPort(Factory(root), store).handle("workbench.backtest", payload)
            items = Research(store).list()

            self.assertEqual(result.data["run_id"], "one")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["run_id"], "one")
            self.assertEqual(items[0]["manifest_file"], "data/runs/one/manifest.json")
            self.assertTrue(store.path("runs", "one", "manifest.json").is_file())
            self.assertFalse((Path(root) / "runs").exists())

    def test_real_result_reaches_existing_ui_port_as_exact_json(self):
        from protocal import KernelBacktestPort, Modules, Protocol

        with TemporaryDirectory() as root:
            backend = Protocol(Modules(KernelBacktestPort(Factory(root), DataStore(Path(root))), *[EmptyPort() for _ in range(4)]))
            message = send(backend, wire_request())
            wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))

        self.assertIsInstance(message, ui.UiMessage)
        self.assertEqual(wire["protocol"], ui.PROTOCOL_NAME)
        self.assertEqual(wire["version"], ui.PROTOCOL_VERSION)
        self.assertEqual(wire["kind"], "resource")
        self.assertEqual(wire["correlation_id"], "one")
        self.assertIsInstance(wire["data"]["final_cash"], str)
        self.assertEqual(Decimal(wire["data"]["final_cash"]), Decimal("97762.92704000"))
        self.assertEqual(wire["data"]["fills"], 16)
        self.assertEqual(wire["data"]["positions"], {})
        self.assertNotIn("results", wire["data"])

    def test_failure_is_a_problem_and_next_request_can_succeed(self):
        from protocal import KernelBacktestPort, Modules, Protocol

        class BrokenFirst(Factory):
            def __init__(self, root):
                super().__init__(root)
                self.opened = 0

            @contextmanager
            def open(self, value):
                with super().open(value) as parts:
                    self.opened += 1
                    if self.opened == 1:
                        parts.source = (object(),)
                    yield parts

        with TemporaryDirectory() as root:
            factory = BrokenFirst(root)
            backend = Protocol(Modules(KernelBacktestPort(factory, DataStore(Path(root))), *[EmptyPort() for _ in range(4)]))
            bad = send(backend, wire_request(request("bad"), "bad"))
            good = send(backend, wire_request(request("good"), "good"))

        self.assertEqual(bad.kind, "problem")
        self.assertEqual(bad.correlation_id, "bad")
        self.assertEqual(bad.data["code"], "run_failed")
        self.assertFalse(bad.data["retryable"])
        self.assertEqual(factory.closed, 2)
        self.assertEqual(good.kind, "resource")
        self.assertNotEqual(good.message_id, bad.message_id)

    def test_invalid_input_is_reported_without_opening_resources(self):
        from protocal import KernelBacktestPort, Modules, Protocol

        with TemporaryDirectory() as root:
            factory = Factory(root)
            backend = Protocol(Modules(KernelBacktestPort(factory, DataStore(Path(root))), *[EmptyPort() for _ in range(4)]))
            value = send(backend, ui.UiRequest("bad", "workbench.backtest", "urn:lianghua:ui:input:workbench.backtest:v1", {}))

        self.assertEqual(value.kind, "problem")
        self.assertEqual(value.data["code"], "invalid_request")
        self.assertEqual(factory.runs, [])

    def test_open_positions_are_lossless_json_strings(self):
        from protocal import KernelBacktestPort, Modules, Protocol

        class Short(Factory):
            @contextmanager
            def open(self, value):
                with super().open(value) as parts:
                    parts.source = frames()[:2]
                    yield parts

        with TemporaryDirectory() as root:
            value = request(end=START + timedelta(days=1))
            backend = Protocol(Modules(KernelBacktestPort(Short(root), DataStore(Path(root))), *[EmptyPort() for _ in range(4)]))
            message = send(backend, wire_request(value))
            data = json.loads(json.dumps(message.to_dict(), allow_nan=False))["data"]

        self.assertEqual(len(data["positions"]), 8)
        self.assertTrue(all(isinstance(item, str) for item in data["positions"].values()))
        self.assertEqual(sum(Decimal(item) for item in data["positions"].values()), Decimal("9600"))


class EmptyPort:
    def handle(self, operation, data):
        return {}
