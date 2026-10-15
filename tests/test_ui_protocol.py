from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch


protocol = importlib.import_module("frontend.ui-protocol")


class UiProtocolTest(unittest.TestCase):
    def test_exchange_closes_backend_when_node_cannot_start(self):
        class Backend:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True

        backend = Backend()
        action = protocol.UiAction("a1", "invoke", "cli", {}, "example.read")
        with patch("protocal.build_backend", return_value=backend), \
             patch.object(protocol.subprocess, "Popen", side_effect=OSError("node missing")):
            with self.assertRaises(OSError):
                protocol.exchange(action)
        self.assertTrue(backend.closed)

    def test_exchange_refreshes_transport_deadline_after_backend_processing(self):
        class Clock:
            value = 0

        class Stream:
            def __init__(self, lines=()):
                self.lines = list(lines)
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                return None

            def readline(self):
                return self.lines.pop(0) if self.lines else ""

            def read(self):
                return ""

            def close(self):
                return None

        state = protocol.UiState(targets={"cli": protocol.UiSlot()})
        request = protocol.UiRequest(
            "r1", "example.read", "urn:example:input", {},
        )
        lines = [
            json.dumps({"kind": "request", "request": request.to_dict(), "messages": []}) + "\n",
            json.dumps({"kind": "state", "state": {
                "revision": 0,
                "targets": {"cli": {"revision": 0, "values": {}, "selection": None,
                                     "query": {}, "messages": [], "problems": [], "operations": {}}},
            }}) + "\n",
        ]
        stdout = Stream(lines)
        process = type("Process", (), {
            "stdin": Stream(), "stdout": stdout, "stderr": Stream(),
            "wait": lambda self, timeout=None: None,
        })()
        class Backend:
            async def send(self, request):
                return []
            def close(self):
                return None

        backend = Backend()
        class Selector:
            timeouts = []
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def register(self, stream, event): return None
            def select(self, timeout):
                self.timeouts.append(timeout)
                return [(stdout, 1)]

        action = protocol.UiAction("a1", "invoke", "cli", {}, "example.read")
        def run_backend(awaitable):
            awaitable.close()
            Clock.value = 31
            return []

        with patch("protocal.build_backend", return_value=backend), \
             patch.object(protocol.subprocess, "Popen", return_value=process), \
             patch.object(protocol.selectors, "DefaultSelector", Selector), \
             patch.object(protocol.asyncio, "run", side_effect=run_backend), \
             patch.object(protocol.time, "monotonic", side_effect=lambda: Clock.value):
            result = protocol.exchange(action)

        self.assertEqual(result, state)
        self.assertGreater(Selector.timeouts[1], 0)

    def test_exchange_kills_a_core_process_that_ignores_terminate(self):
        class Process:
            def __init__(self):
                self.terminated = False
                self.killed = False
                self.waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits < 3:
                    raise protocol.subprocess.TimeoutExpired("node", timeout)
                return -9
            def terminate(self): self.terminated = True
            def kill(self): self.killed = True

        process = Process()
        protocol._close_process(process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, 3)

    def test_frontend_actions_and_backend_requests_have_distinct_ports(self):
        action = protocol.UiAction(action_id='a1', kind='invoke', target='form',
                                   operation='example.save', data={'amount': '12.50'})
        request = protocol.UiRequest(request_id='r1', operation='example.save',
                                     schema='urn:example:input', data=action.data)
        self.assertEqual('invoke', action.to_dict()['kind'])
        self.assertEqual('12.50', request.to_dict()['data']['amount'])
        for name in ('dispatch', 'snapshot', 'subscribe'):
            self.assertTrue(hasattr(protocol.FrontendPort, name))
        self.assertTrue(hasattr(protocol.BackendPort, 'send'))

    def test_action_rejects_missing_operation_and_non_json_values(self):
        with self.assertRaises(ValueError):
            protocol.UiAction(action_id='a1', kind='invoke', target='form', data={})
        with self.assertRaises(TypeError):
            protocol.UiAction(action_id='a1', kind='edit', target='form', data={'x': float('nan')})

    def test_schema_exports_recursive_json_and_both_protocol_faces(self):
        schema = protocol.export_schema()
        self.assertEqual('https://json-schema.org/draft/2020-12/schema', schema['$schema'])
        for name in ('UiAction', 'UiRequest', 'UiState', 'UiMessage', 'UiProblem', 'UiOperation'):
            self.assertIn(name, schema['$defs'])
        json.dumps(schema, allow_nan=False)

    def test_shared_wire_samples(self):
        path = Path(__file__).resolve().parents[1] / 'frontend/display/tests/wire.json'
        for sample in json.loads(path.read_text()):
            with self.subTest(sample=sample['name']):
                if sample['valid']:
                    protocol.parse_wire(sample['model'], sample['value'])
                else:
                    with self.assertRaises((ValueError, TypeError)):
                        protocol.parse_wire(sample['model'], sample['value'])

    def test_message_serializes_as_generic_json_envelope(self) -> None:
        message = protocol.UiMessage(
            message_id="m-1",
            correlation_id="run-1",
            kind="resource",
            type="urn:lianghua:account",
            schema="https://example.test/schemas/account.json",
            data={"id": "account-1", "balance": 12.5},
        )

        value = message.to_dict()

        self.assertEqual(protocol.PROTOCOL_NAME, value["protocol"])
        self.assertEqual(protocol.PROTOCOL_VERSION, value["version"])
        self.assertEqual("resource", value["kind"])
        self.assertEqual("run-1", value["correlation_id"])
        self.assertEqual({"id": "account-1", "balance": 12.5}, value["data"])
        json.dumps(value, allow_nan=False)

    def test_presentation_contains_semantic_hints_only(self) -> None:
        message = protocol.UiMessage(
            message_id="m-2",
            kind="collection",
            type="urn:example:items",
            schema="urn:example:schema:items",
            data=[{"name": "A"}],
            presentation=protocol.Presentation(
                title="Items",
                role="collection",
                fields=(
                    protocol.FieldHint(
                        path="/0/name",
                        label="Name",
                        role="text",
                        format="plain",
                        order=1,
                    ),
                ),
            ),
        )

        presentation = message.to_dict()["presentation"]

        self.assertEqual("Items", presentation["title"])
        self.assertEqual("/0/name", presentation["fields"][0]["path"])
        self.assertNotIn("component", presentation)
        self.assertNotIn("layout", presentation)
        self.assertNotIn("css", presentation)

    def test_problem_is_a_normal_message_kind(self) -> None:
        problem = protocol.UiProblem(
            type="urn:example:validation-error",
            code="invalid-input",
            title="Invalid input",
            detail="The value is not valid.",
            field="amount",
            retryable=False,
        )
        message = protocol.UiMessage(
            message_id="m-3",
            correlation_id="request-1",
            kind="problem",
            type=problem.type,
            schema="urn:example:schema:problem",
            data=problem.to_dict(),
        )

        value = message.to_dict()

        self.assertEqual("problem", value["kind"])
        self.assertEqual("invalid-input", value["data"]["code"])
        self.assertEqual("amount", value["data"]["field"])

    def test_event_can_carry_links_and_namespaced_extensions(self) -> None:
        message = protocol.UiMessage(
            message_id="m-4",
            kind="event",
            type="urn:example:progress",
            schema="urn:example:schema:progress",
            data={"progress": 0.5},
            links=(protocol.UiLink(rel="self", href="urn:example:event:m-4"),),
            extensions={"urn:example:trace": {"span": "s-1"}},
        )

        value = message.to_dict()

        self.assertEqual("self", value["links"][0]["rel"])
        self.assertEqual("s-1", value["extensions"]["urn:example:trace"]["span"])

    def test_message_rejects_non_json_data(self) -> None:
        with self.assertRaises(TypeError):
            protocol.UiMessage(
                message_id="m-5",
                kind="resource",
                type="urn:example:object",
                schema="urn:example:schema:object",
                data=object(),
            )

        with self.assertRaises(TypeError):
            protocol.UiMessage(
                message_id="m-5b",
                kind="resource",
                type="urn:example:object",
                schema="urn:example:schema:object",
                data={"value": float("nan")},
            )

    def test_message_rejects_empty_identity_fields(self) -> None:
        with self.assertRaises(ValueError):
            protocol.UiMessage(
                message_id="",
                kind="resource",
                type="urn:example:object",
                schema="urn:example:schema:object",
                data={},
            )

        with self.assertRaises(ValueError):
            protocol.UiMessage(
                message_id="m-6",
                kind="resource",
                type="",
                schema="urn:example:schema:object",
                data={},
            )

    def test_message_rejects_unnamespaced_extensions(self) -> None:
        with self.assertRaises(ValueError):
            protocol.UiMessage(
                message_id="m-7",
                kind="custom",
                type="urn:example:custom",
                schema="urn:example:schema:custom",
                data={},
                extensions={"debug": True},
            )

    def test_message_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            protocol.UiMessage(
                message_id="m-8",
                kind="unknown",
                type="urn:example:unknown",
                schema="urn:example:schema:unknown",
                data={},
            )

    def test_protocol_port_accepts_messages(self) -> None:
        self.assertTrue(hasattr(protocol.UiProtocol, "receive"))


if __name__ == "__main__":
    unittest.main()
