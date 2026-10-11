import asyncio
import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import asdict

from tests.test_core_backtest import Factory, request


ui = importlib.import_module("frontend.ui-protocol")


class Port:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def handle(self, operation, data):
        self.calls.append((operation, data))
        return self.value


class ProtocolBoundaryTests(unittest.TestCase):
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

    def test_backtest_port_is_the_only_protocol_owned_kernel_adapter(self):
        from protocal import KernelBacktestPort, Modules, Protocol

        with TemporaryDirectory() as root:
            protocol = Protocol(Modules(
                backtests=KernelBacktestPort(Factory(root)),
                research=Port({}), sources=Port({}), lake=Port({}), commands=Port({}),
            ))
            request = ui.UiRequest(
                "request-2", "workbench.backtest", "urn:lianghua:ui:input:workbench.backtest:v1",
                {"run_id": "one", "module": "breadth_low_amount_reversal",
                 "params": {}, "dataset_id": "sample",
                 "start": "2026-09-28T07:00:00+00:00", "end": "2026-10-01T07:00:00+00:00",
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
