import asyncio
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.history_fixture import make_snapshot

ui = importlib.import_module("frontend.ui-protocol")


def send(protocol, request):
    return asyncio.run(protocol.send(request))[0]


class EmptyPort:
    def handle(self, operation, data):
        return {}


class ProtocolTests(unittest.TestCase):
    def test_kernel_result_is_saved_once_in_the_shared_data_store(self):
        from history_data import HistoricalLake
        from plugins.data import Settlement
        from protocal import DataBus, KernelBacktestPort
        from storage import DataStore

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            bus.configure_reader(HistoricalLake(bus, config))
            port = KernelBacktestPort(bus, Settlement)
            result = port.handle("workbench.backtest", {
                "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
            })
            saved = json.loads(bus.read("runs", "one", "result.json"))

        self.assertEqual(result.data["run_id"], "one")
        self.assertEqual(saved["trace"], result.data["trace"])
        self.assertEqual(saved["equity"], result.data["equity"])

    def test_protocol_routes_backtest_to_the_injected_port(self):
        from history_data import HistoricalLake
        from plugins.data import Settlement
        from protocal import DataBus, KernelBacktestPort, Modules, Protocol
        from storage import DataStore

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            bus.configure_reader(HistoricalLake(bus, config))
            backend = Protocol(Modules(
                KernelBacktestPort(bus, Settlement), *[EmptyPort() for _ in range(4)]))
            message = send(backend, ui.UiRequest(
                "one", "workbench.backtest", "urn:lianghua:ui:input:workbench.backtest:v1", {
                    "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                    "params": {"positions": 4}, "dataset_id": "fixture",
                    "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                }))

        wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))
        self.assertEqual(message.kind, "resource")
        self.assertEqual(wire["data"]["run_id"], "one")
        self.assertIn("provenance", wire["data"])
