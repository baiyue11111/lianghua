"""CLI adapter tests use only the public UI contract and the built Node core."""
import contextlib
import importlib
import io
import json
import unittest
from pathlib import Path
from frontend import cli
protocol=importlib.import_module('frontend.ui-protocol')

class CliTest(unittest.TestCase):
    def test_help_and_empty_args(self):
        for values in ([],['--help'],['run','--help'],['ingest','--help'],['report','--help']):
            with contextlib.redirect_stdout(io.StringIO()):
                try: result=cli.main(values)
                except SystemExit as exc: result=exc.code
            self.assertEqual(result,0)

    def test_all_commands_become_actions_without_calling_business_services(self):
        parser=cli.build_parser()
        commands=[['run','--config','x.json'],['ingest','--price','p','--fundamentals','f','--valuations','v','--output','o'],['report','--artifact','a','--output','o','--builder','b']]
        for values in commands:
            action=cli.to_action(parser.parse_args(values))
            self.assertEqual(action.kind,'invoke')
            self.assertEqual(action.operation,'cli.'+values[0])
            protocol.parse_wire('UiAction',action.to_dict())

    def test_unbound_backend_is_an_explicit_error(self):
        error=io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(cli.main(['run','--config','does-not-exist']),2)
        self.assertIn('未连接',error.getvalue())

    def test_standard_backend_is_injected_and_exact_decimal_survives(self):
        requests=[]
        class Backend:
            async def send(self,request):
                requests.append(request)
                return [protocol.UiMessage('result','resource','urn:test',f'urn:lianghua:ui:output:{request.operation}:v1',{'amount':'9007199254740993.10'},correlation_id=request.request_id)]
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['run','--config','x.json'],backend=Backend()),0)
        self.assertEqual(requests[0].operation,'cli.run')
        self.assertEqual(json.loads(output.getvalue()),{'amount':'9007199254740993.10'})

    def test_backend_failure_returns_problem_and_reaps_process(self):
        class Backend:
            async def send(self,request): raise ValueError('offline')
        output=io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(['run','--config','x.json'],backend=Backend()),2)
        self.assertIn('offline',output.getvalue())
