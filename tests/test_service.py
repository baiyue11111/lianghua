import asyncio
import json
import plistlib
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

    def test_dev_reuses_a_matching_resident_service(self):
        with TemporaryDirectory() as folder, patch('dev.service_root', return_value=Path(folder).resolve()) as probe, \
                patch('dev.subprocess.run') as run:
            dev.ensure_service(Path(folder))
        probe.assert_called_once()
        run.assert_not_called()

    def test_service_probe_reads_identity_from_the_standard_protocol(self):
        message = dev.ui.UiMessage('m', 'resource', 'urn:test',
            'urn:lianghua:ui:output:service.info:v1', {'root': '/tmp/project'},
            correlation_id='service-check')
        with patch.object(dev.ui, '_send_backend', return_value=[message]) as call:
            self.assertEqual(dev.service_root(), Path('/tmp/project').resolve())
        self.assertEqual(call.call_args.args[0].operation, 'service.info')
        self.assertEqual(call.call_args.kwargs['timeout'], 2)

    def test_service_probe_rejects_an_unrelated_server(self):
        message = dev.ui.UiMessage('m', 'resource', 'urn:test',
            'urn:lianghua:ui:output:service.info:v1', {}, correlation_id='service-check')
        with patch.object(dev.ui, '_send_backend', return_value=[message]):
            with self.assertRaisesRegex(RuntimeError, '身份'):
                dev.service_root()

    def test_dev_rejects_resident_service_for_another_project(self):
        with TemporaryDirectory() as folder, patch('dev.service_root', return_value=Path('/other')):
            with self.assertRaisesRegex(RuntimeError, '项目'):
                dev.ensure_service(Path(folder))

    def test_dev_starts_the_installed_service_when_it_is_down(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with patch('dev.service_root', side_effect=[None, root]), \
                    patch('dev.subprocess.run') as run:
                dev.ensure_service(root)
        self.assertEqual(run.call_args.args[0],
                         ['launchctl', 'kickstart', f'gui/{dev.os.getuid()}/org.lianghua.backend'])


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
