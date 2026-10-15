"""CLI adapter tests use only the public UI contract and the built Node core."""
import contextlib
import ast
import importlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from frontend import cli
protocol=importlib.import_module('frontend.ui-protocol')

class CliTest(unittest.TestCase):
    def test_help_and_empty_args(self):
        for values in ([],['--help'],['ingest','--help'],['report','--help']):
            with contextlib.redirect_stdout(io.StringIO()):
                try: result=cli.main(values)
                except SystemExit as exc: result=exc.code
            self.assertEqual(result,0)

    def test_all_commands_become_actions_without_calling_business_services(self):
        parser=cli.build_parser()
        commands=[['--backtest','x.json'],['ingest','--price','p','--fundamentals','f','--valuations','v','--output','o'],['report','--artifact','a','--output','o','--builder','b']]
        for values in commands:
            action=cli.to_action(parser.parse_args(values))
            self.assertEqual(action.kind,'invoke')
            self.assertEqual(action.operation,'cli.backtest' if values[0]=='--backtest' else 'cli.'+values[0])
            if values[0]=='--backtest': self.assertEqual(action.data,{'config':'x.json'})
            protocol.parse_wire('UiAction',action.to_dict())

    def test_old_subcommand_is_not_a_cli_fallback(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['run','--config','x.json'])

    def test_backtest_cannot_silently_ignore_ingest_or_report(self):
        with self.assertRaises(SystemExit):
            cli.main(['--backtest','x.json','ingest','--price','p','--fundamentals','f','--valuations','v','--output','o'])

    def test_cli_does_not_import_business_assembly(self):
        root=Path(__file__).resolve().parents[1]
        tree=ast.parse((root/'frontend/cli.py').read_text())
        names={alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names}
        names.update(node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom))
        source=(root/'frontend/cli.py').read_text()
        self.assertNotIn('build_backend', source)
        self.assertNotIn('core.kernel', source)
        self.assertNotIn('build_backend', names)

    def test_shared_runtime_preserves_exact_decimal_message(self):
        message=protocol.UiMessage('result','resource','urn:test','urn:lianghua:ui:output:cli.backtest:v1',{'amount':'9007199254740993.10'})
        state=protocol.UiState(targets={'cli':protocol.UiSlot(messages=(message,))})
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            with patch.object(protocol, 'exchange', return_value=state) as exchange:
                self.assertEqual(cli.main(['--backtest','x.json']),0)
        self.assertEqual(exchange.call_args.args[0].operation,'cli.backtest')
        self.assertEqual(json.loads(output.getvalue()),{'amount':'9007199254740993.10'})

    def test_backend_failure_returns_problem_and_reaps_process(self):
        output=io.StringIO()
        with contextlib.redirect_stderr(output):
            with patch.object(protocol, 'exchange', side_effect=ValueError('offline')):
                self.assertEqual(cli.main(['--backtest','x.json']),2)
        self.assertIn('offline',output.getvalue())

    def test_console_script_uses_lianghua_name(self):
        text=(Path(__file__).resolve().parents[1]/'pyproject.toml').read_text()
        self.assertIn('lianghua = "frontend.cli:main"',text)
        self.assertNotIn('lianghua-backtest = "frontend.cli:main"',text)
