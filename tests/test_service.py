import asyncio
import json
import plistlib
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import dev
from frontend.web import create_app


class Backend:
    def __init__(self):
        self.calls = []

    async def send(self, request):
        self.calls.append(('send', request.operation))
        return []

    def start(self):
        self.calls.append('start')

    def close(self):
        self.calls.append('close')


class ServiceTests(unittest.TestCase):
    def test_web_lifecycle_starts_and_stops_the_backend(self):
        backend = Backend()
        app = create_app(backend)

        async def lifecycle():
            async with app.router.lifespan_context(app):
                self.assertEqual(backend.calls, ['start'])
            self.assertEqual(backend.calls, ['start', 'close'])

        asyncio.run(lifecycle())

    def test_slow_request_does_not_block_a_status_request(self):
        from backend import Modules, Protocol

        started, release = threading.Event(), threading.Event()

        class Port:
            def handle(self, operation, data):
                if operation == 'cli.backtest':
                    started.set()
                    release.wait(1)
                    return {'done': True}
                return {'online': True}

        app = create_app(Protocol(Modules(*(Port() for _ in range(5)))))

        async def request(operation, identity):
            body = json.dumps({'request_id': identity, 'operation': operation,
                'schema': f'urn:lianghua:ui:input:{operation}:v1', 'data': {}}).encode()
            sent = []

            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}

            async def send(message):
                sent.append(message)

            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'method': 'POST',
                'path': '/api/protocol', 'raw_path': b'/api/protocol', 'query_string': b'',
                'headers': [(b'content-type', b'application/json'),
                            (b'host', b'127.0.0.1:8000')], 'scheme': 'http',
                'server': ('test', 80), 'client': ('test', 1), 'root_path': ''}
            await app(scope, receive, send)
            return json.loads(b''.join(row.get('body', b'') for row in sent
                                       if row['type'] == 'http.response.body'))

        async def exercise():
            slow = asyncio.create_task(request('cli.backtest', 'slow'))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                self.assertTrue(started.is_set())
                status = await asyncio.wait_for(request('data.status', 'status'), timeout=.2)
                self.assertEqual(status[0]['data'], {'online': True})
                self.assertFalse(slow.done(), 'long request blocked the event loop')
            finally:
                release.set()
                await slow

        asyncio.run(exercise())

    def test_launch_agent_uses_one_persistent_backend_for_this_project(self):
        from tools.launchagent import build

        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / '.venv/bin').mkdir(parents=True)
            (root / '.venv/bin/python').touch()
            config = plistlib.loads(build(root))
            self.assertEqual(config['Label'], 'org.lianghua.backend')
            self.assertEqual(config['WorkingDirectory'], str(root))
            self.assertEqual(config['ProgramArguments'], [str(root / '.venv/bin/python'),
                '-m', 'uvicorn', 'dev:create_app', '--factory', '--host', '127.0.0.1', '--port', '8000'])
            self.assertTrue(config['RunAtLoad'])
            self.assertTrue(config['KeepAlive'])

    def test_ensure_reuses_a_port_that_is_already_answered(self):
        with patch('dev.port_open', return_value=True), patch('dev.subprocess.Popen') as popen:
            processes = []
            dev.ensure('127.0.0.1', 8000, ['whatever'], Path('.'), processes)
        popen.assert_not_called()
        self.assertEqual(processes, [])

    def test_ensure_starts_a_command_when_the_port_is_free(self):
        with patch('dev.port_open', side_effect=[False, True]), \
                patch('dev.wait_for_port') as wait, patch('dev.subprocess.Popen') as popen:
            processes = []
            dev.ensure('127.0.0.1', 8000, ['python', '-m', 'uvicorn'], Path('/proj'), processes)
        self.assertEqual(popen.call_args.args[0], ['python', '-m', 'uvicorn'])
        self.assertEqual(popen.call_args.kwargs['cwd'], Path('/proj'))
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        wait.assert_called_once_with(popen.return_value, '127.0.0.1', 8000)
        self.assertEqual(processes, [popen.return_value])

    def test_ensure_starts_nothing_when_the_process_dies_is_reported_by_wait(self):
        with patch('dev.port_open', return_value=False), \
                patch('dev.wait_for_port', side_effect=RuntimeError('退出码 3')), \
                patch('dev.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, '退出码 3'):
                dev.ensure('127.0.0.1', 8000, ['whatever'], Path('.'), [])
        popen.assert_called_once()

    def test_catalog_probe_names_the_process_that_holds_it(self):
        # DuckDB is embedded and the writer takes an exclusive lock, so a probe
        # that cannot name the holder cannot tell "busy" from "broken" — and
        # "busy" is the case that should wait rather than fail.
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog.duckdb'
            writer = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
                import duckdb, sys
                connection = duckdb.connect({str(path)!r})
                connection.execute("create table t as select 1")
                connection.execute("checkpoint")
                print("ready", flush=True)
                sys.stdin.read()
            ''')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(writer.stdout.readline().strip(), 'ready')
                self.assertEqual(dev.catalog_holder(path), str(writer.pid))
            finally:
                writer.stdin.close()
                writer.wait(timeout=30)
                writer.stdout.close()
            self.assertIsNone(dev.catalog_holder(path))

    def test_a_missing_catalog_is_ready_without_waiting(self):
        with TemporaryDirectory() as folder:
            dev.require_catalog(Path(folder) / 'catalog.duckdb')

    def test_a_free_catalog_is_ready(self):
        with patch('dev.catalog_holder', return_value=None):
            dev.require_catalog(Path('data/catalog.duckdb'))

    def test_a_busy_catalog_is_refused_naming_the_holder(self):
        # Nothing but a migration tool opens the file from outside any more, so
        # the answer is a sentence naming the process — not a traceback and not
        # a wait on a person.
        with patch('dev.catalog_holder', return_value='46839'), \
                patch('dev.describe', return_value='uv run migrate_store.py'):
            with self.assertRaisesRegex(RuntimeError, '46839'):
                dev.require_catalog(Path('data/catalog.duckdb'))

    def _post(self, app, body: bytes, host: str | None = '127.0.0.1:8000',
              origin: str | None = None, content_type: str = 'application/json'):
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            sent.append(message)

        headers = [(b'content-type', content_type.encode())]
        if host:
            headers.append((b'host', host.encode()))
        if origin:
            headers.append((b'origin', origin.encode()))
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'method': 'POST',
            'path': '/api/protocol', 'raw_path': b'/api/protocol', 'query_string': b'',
            'headers': headers, 'scheme': 'http', 'server': ('test', 80),
            'client': ('test', 1), 'root_path': ''}
        asyncio.run(app(scope, receive, send))
        status = next(row['status'] for row in sent if row['type'] == 'http.response.start')
        response_headers = {name.decode().lower(): value.decode()
                            for name, value in sent[0].get('headers', [])}
        payload = b''.join(row.get('body', b'') for row in sent
                           if row['type'] == 'http.response.body')
        return status, json.loads(payload), response_headers

    def test_malformed_request_comes_back_as_a_problem_message(self):
        # A transport that answers 500 with an empty body makes "bad request" and
        # "backend broken" indistinguishable, and the client can only retry blindly.
        app = create_app(Backend())
        status, payload, _headers = self._post(app, b'{"nonsense":true}')
        self.assertEqual(status, 400)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['kind'], 'problem')
        self.assertEqual(payload[0]['data']['code'], 'invalid_request')

    def test_body_that_is_not_json_also_comes_back_as_a_problem(self):
        app = create_app(Backend())
        status, payload, _headers = self._post(app, b'definitely not json')
        self.assertEqual(status, 400)
        self.assertEqual(payload[0]['kind'], 'problem')

    def test_an_operation_over_its_budget_comes_back_retryable(self):
        # A request with no deadline holds the caller forever: the browser gives
        # up, the work keeps running, and the next request queues behind it. The
        # deadline bounds the wait; a thread cannot be cancelled, so the work
        # itself runs to completion — which is why the operation must also be
        # cheap, not merely bounded.
        import threading
        import time
        from types import SimpleNamespace
        from frontend.web import _OverBudget, _run

        done = threading.Event()

        class Slow(Backend):
            async def send(self, request):
                time.sleep(.5)
                done.set()
                return []

        async def exercise():
            with patch('frontend.web.BUDGET', .05):
                with self.assertRaises(_OverBudget):
                    await _run(Slow(), SimpleNamespace(operation='research.runs'))
            self.assertFalse(done.is_set(), 'the caller waited for the operation')
            await asyncio.sleep(.6)                # let the thread drain

        asyncio.run(exercise())

    def test_a_budget_exhaustion_is_reported_as_a_retryable_problem(self):
        from frontend.web import _OverBudget

        body = json.dumps({'request_id': 'r', 'operation': 'research.runs',
            'schema': 'urn:lianghua:ui:input:research.runs:v1', 'data': {}}).encode()
        app = create_app(Backend())
        with patch('frontend.web._run', side_effect=_OverBudget('research.runs')):
            status, payload, _headers = self._post(app, body)
        self.assertEqual(status, 504)
        self.assertEqual(payload[0]['data']['code'], 'timeout')
        self.assertTrue(payload[0]['data']['retryable'])

    def test_transport_serves_loopback_hosts_only(self):
        # DNS rebinding keeps the attacker hostname and moves the address to
        # 127.0.0.1, so the Host header is the check that still discriminates.
        app = create_app(Backend())
        body = json.dumps({'request_id': 'r', 'operation': 'data.status',
            'schema': 'urn:lianghua:ui:input:data.status:v1', 'data': {}}).encode()
        status, payload, _headers = self._post(app, body, host='rebind.attacker.example:8000')
        self.assertEqual(status, 403)
        self.assertEqual(payload[0]['kind'], 'problem')
        for allowed in ('127.0.0.1:8000', 'localhost:3000', '[::1]:8000'):
            with self.subTest(host=allowed):
                status, _payload, _headers = self._post(app, body, host=allowed)
                self.assertEqual(status, 200)

    def test_transport_grants_no_cross_origin_read(self):
        # The UI reaches the API same-origin through the dev proxy, so a CORS
        # grant would only widen the audience to strangers' web pages.
        app = create_app(Backend())
        body = json.dumps({'request_id': 'r', 'operation': 'data.status',
            'schema': 'urn:lianghua:ui:input:data.status:v1', 'data': {}}).encode()
        _status, _payload, headers = self._post(app, body, host='localhost:3000',
                                               origin='https://evil.example')
        self.assertNotIn('access-control-allow-origin', headers)

    def test_a_cross_site_request_never_reaches_the_protocol(self):
        # A page can always POST to 127.0.0.1, and it will do so as a simple
        # request — no preflight, no CORS decision — when the body is declared
        # text/plain. The Host header is 127.0.0.1 either way, so only the
        # Origin and the media type can tell an operator apart from a stranger.
        backend = Backend()
        app = create_app(backend)
        body = json.dumps({'request_id': 'r', 'operation': 'data.status',
            'schema': 'urn:lianghua:ui:input:data.status:v1', 'data': {}}).encode()
        cases = (
            {'origin': 'https://evil.example'},
            {'origin': 'https://evil.example', 'content_type': 'text/plain'},
            {'origin': 'http://127.0.0.1.evil.example'},
            {'origin': 'null'},
            {'content_type': 'text/plain'},
            {'content_type': 'application/x-www-form-urlencoded'},
            {'content_type': ''},
        )
        for case in cases:
            with self.subTest(**case):
                status, payload, _headers = self._post(app, body, **case)
                self.assertIn(status, (403, 415))
                self.assertEqual(payload[0]['kind'], 'problem')
        self.assertEqual(backend.calls, [])

    def test_a_loopback_origin_is_served(self):
        # The dev proxy forwards the browser's Origin unchanged.
        backend = Backend()
        app = create_app(backend)
        body = json.dumps({'request_id': 'r', 'operation': 'data.status',
            'schema': 'urn:lianghua:ui:input:data.status:v1', 'data': {}}).encode()
        for origin in ('http://localhost:5173', 'http://127.0.0.1:5173', 'http://[::1]:5173'):
            with self.subTest(origin=origin):
                status, _payload, _headers = self._post(app, body, origin=origin)
                self.assertEqual(status, 200)
        # A media type parameter is still JSON.
        status, _payload, _headers = self._post(
            app, body, content_type='application/json; charset=utf-8')
        self.assertEqual(status, 200)

    def test_a_missing_host_is_refused_rather_than_trusted(self):
        # Absence of a check is not permission. HTTP/1.1 always carries Host, so
        # a request without one did not come from a browser talking to us.
        backend = Backend()
        app = create_app(backend)
        body = json.dumps({'request_id': 'r', 'operation': 'data.status',
            'schema': 'urn:lianghua:ui:input:data.status:v1', 'data': {}}).encode()
        status, payload, _headers = self._post(app, body, host=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload[0]['kind'], 'problem')
        self.assertEqual(backend.calls, [])

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        from frontend.web import BODY_LIMIT

        backend = Backend()
        app = create_app(backend)
        body = b'{"pad":"' + b'x' * BODY_LIMIT + b'"}'
        status, payload, _headers = self._post(app, body)
        self.assertEqual(status, 413)
        self.assertEqual(payload[0]['kind'], 'problem')
        self.assertEqual(backend.calls, [])


if __name__ == '__main__':
    unittest.main()
