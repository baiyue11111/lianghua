"""CLI talks to the resident backend through the shared interaction core."""
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ui = importlib.import_module('frontend.ui-protocol')


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200
    def read(self):
        return json.dumps(self.payload).encode()


def connection(respond):
    class Socket:
        def settimeout(self, value):
            pass
    class Connection:
        def __init__(self, host, port, timeout):
            self.sock = Socket()
        def connect(self):
            pass
        def request(self, method, path, body, headers):
            self.response = respond(json.loads(body))
        def getresponse(self):
            return self.response
        def close(self):
            pass
    return Connection


class CliTransportTests(unittest.TestCase):
    def test_connection_timeout_does_not_limit_long_responses(self):
        request = ui.UiRequest('r', 'service.info', 'urn:lianghua:ui:input:service.info:v1', {})
        reply = ui.UiMessage('m', 'resource', 'urn:test', 'urn:lianghua:ui:output:service.info:v1',
                             {'root': str(Path.cwd())}, correlation_id='r')
        with patch('http.client.HTTPConnection') as factory:
            connection = factory.return_value
            connection.getresponse.return_value.status = 200
            connection.getresponse.return_value.read.return_value = json.dumps([reply.to_dict()]).encode()
            result = ui._send_backend(request)
        factory.assert_called_once_with('127.0.0.1', 8000, timeout=5)
        connection.connect.assert_called_once_with()
        connection.sock.settimeout.assert_called_once_with(None)
        connection.close.assert_called_once_with()
        self.assertEqual(result[0].data, {'root': str(Path.cwd())})

    def test_core_identifies_service_then_sends_operation(self):
        calls = []
        def respond(data):
            calls.append(data)
            value = {'root': str(Path.cwd())} if data['operation'] == 'service.info' else {'ok': True}
            message = ui.UiMessage('m', 'resource', 'urn:test',
                f"urn:lianghua:ui:output:{data['operation']}:v1", value,
                correlation_id=data['request_id'])
            return Response([message.to_dict()])
        action = ui.UiAction('a', 'invoke', 'cli', {'config': '/tmp/x.json'}, 'cli.backtest')
        with patch('http.client.HTTPConnection', connection(respond)), \
             patch('backend.build_backend', side_effect=AssertionError('unexpected local backend')):
            state = ui.exchange(action)
        self.assertEqual([item['operation'] for item in calls], ['service.info', 'cli.backtest'])
        self.assertEqual(state.targets['cli'].messages[0].data, {'ok': True})

    def test_wrong_project_is_rejected_before_user_operation(self):
        calls = []
        def respond(data):
            calls.append(data['operation'])
            message = ui.UiMessage('m', 'resource', 'urn:test',
                'urn:lianghua:ui:output:service.info:v1', {'root': '/wrong-project'},
                correlation_id=data['request_id'])
            return Response([message.to_dict()])
        with patch('http.client.HTTPConnection', connection(respond)), \
             patch('backend.build_backend', side_effect=AssertionError('unexpected local backend')):
            with self.assertRaisesRegex(ValueError, '项目'):
                ui.exchange(ui.UiAction('a', 'invoke', 'cli', {'config': '/tmp/x.json'}, 'cli.backtest'))
        self.assertEqual(calls, ['service.info'])

    def test_automatic_controls_use_core_registered_operations(self):
        calls = []
        def respond(data):
            calls.append((data['operation'], data['data']))
            value = {'root': str(Path.cwd())} if data['operation'] == 'service.info' else {'enabled': True}
            message = ui.UiMessage('m', 'resource', 'urn:test',
                f"urn:lianghua:ui:output:{data['operation']}:v1", value,
                correlation_id=data['request_id'])
            return Response([message.to_dict()])
        with patch('http.client.HTTPConnection', connection(respond)):
            state = ui.exchange(ui.UiAction('a', 'invoke', 'cli', {'selector': '沪深:auto-on'}, 'cli.collect'))
        self.assertEqual(calls, [('service.info', {}), ('collection.set_automatic', {'market': '沪深', 'enabled': True})])
        self.assertEqual(state.targets['cli'].messages[0].data, {'enabled': True})

    def test_unreachable_service_does_not_create_a_local_backend(self):
        action = ui.UiAction('a', 'invoke', 'cli', {'selector': '沪深:status'}, 'cli.collect')
        with patch('http.client.HTTPConnection') as factory, \
             patch('backend.build_backend', side_effect=AssertionError('unexpected local backend')):
            factory.return_value.connect.side_effect = TimeoutError('offline')
            with self.assertRaisesRegex(ConnectionError, 'offline'):
                ui.exchange(action)
        factory.return_value.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
