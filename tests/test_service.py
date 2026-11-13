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
                'headers': [(b'content-type', b'application/json')], 'scheme': 'http',
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


if __name__ == '__main__':
    unittest.main()
