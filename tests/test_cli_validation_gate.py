import subprocess
import sys
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class CliValidationGateTests(unittest.TestCase):
    def test_real_cli_preserves_decimal_capital_from_numeric_json(self):
        import json
        from tests.history_fixture import make_snapshot
        from protocal import build_data

        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            make_snapshot(root)
            data = build_data(root)
            data.write("sources", "registry", "sources.json", json.dumps({
                "sources": [], "datasets": [{"id": "fixture"}], "benchmarks": [],
            }).encode())
            config = root / "config.json"
            config.write_text(
                '{"module":"plugins.strategies.moving_average_cross",'
                '"params":{"fast_window":1,"slow_window":2},"dataset_id":"fixture",'
                '"start":"2020-01-31","end":"2020-01-31",'
                '"initial_cash":9007199254740993.10}'
            )
            env = {**os.environ, "PYTHONPATH": f"{project}/src:{project}"}
            result = subprocess.run([
                sys.executable, "-m", "frontend.cli", "--backtest", str(config),
            ], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["effective_config"]["initial_cash"],
                             "9007199254740993.10")

    def test_real_cli_runs_engineering_backtest_subprocess_with_exact_capital(self):
        import asyncio
        import json
        from tests.history_fixture import make_snapshot
        from protocal import build_backend, build_data
        ui = __import__("frontend.ui-protocol", fromlist=["UiRequest"])

        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            make_snapshot(root)
            data = build_data(root)
            data.write("sources", "registry", "sources.json", json.dumps({
                "sources": [], "datasets": [{"id": "fixture"}], "benchmarks": [],
            }).encode())
            config = root / "config.json"
            config.write_text(json.dumps({
                "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-01-31",
                "validation": {"start": "2020-02-03", "end": "2020-02-03"},
                "out_of_sample": {"start": "2020-02-04", "end": "2020-02-04"},
                "initial_cash": 1000000,
            }))
            env = {**os.environ, "PYTHONPATH": f"{project}/src:{project}"}
            result = subprocess.run([
                sys.executable, "-m", "frontend.cli", "--backtest", str(config),
            ], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            output = json.loads(result.stdout)
            self.assertEqual(output["effective_config"]["initial_cash"], "1000000")
            self.assertEqual(set(output["segments"]), {"validation", "out_of_sample"})
            self.assertNotIn("9007199254740992", result.stdout)
            self.assertTrue((root / "data" / "result").exists())
            backend = build_backend(root)
            try:
                messages = asyncio.run(backend.send(ui.UiRequest(
                    "run-query", "workbench.run",
                    "urn:lianghua:ui:input:workbench.run:v1",
                    {"id": output["run_id"]},
                )))
            finally:
                backend.close()
            self.assertEqual(messages[0].kind, "resource")
            self.assertEqual(messages[0].data["segments"], output["segments"])

    def test_real_cli_rejects_invalid_backtest_configs_with_clear_errors(self):
        project=Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            root=Path(folder)
            cases={
                'missing': (root/'missing.json', 'module_failed'),
                'invalid_json': (root/'invalid.json', 'Expecting property name'),
                'request_wrapper': (root/'wrapped.json', 'request wrapper'),
                'unknown_field': (root/'unknown.json', 'unsupported request fields'),
            }
            (root/'invalid.json').write_text('{', encoding='utf-8')
            (root/'wrapped.json').write_text('{"request": {}}', encoding='utf-8')
            (root/'unknown.json').write_text('{"module":"x","unexpected":1}', encoding='utf-8')
            env={**os.environ,'PYTHONPATH':f'{project}/src:{project}'}
            for name,(config,needle) in cases.items():
                with self.subTest(name=name):
                    result=subprocess.run([
                        sys.executable,'-m','frontend.cli','--backtest',str(config),
                    ],cwd=project,env=env,capture_output=True,text=True)
                    self.assertEqual(result.returncode,2)
                    self.assertEqual(result.stdout,'')
                    self.assertIn(needle,result.stderr)
                    self.assertNotIn('未连接',result.stderr)
                    self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__': unittest.main()

class PluginCliTests(unittest.TestCase):
    def test_configured_empty_history_gate_creates_no_output(self):
        import json
        with TemporaryDirectory() as folder:
            root=Path(folder); config=root/'run.json'; output=root/'data/result'
            config.write_text(json.dumps({'module':'plugins.strategies.breadth_low_amount_reversal',
                'params':{'positions':12},'initial_cash':100000,'start':'2020-01-01','end':'2020-12-31','dataset_id':''},
                ))
            project=Path(__file__).resolve().parents[1]
            env={**os.environ,'PYTHONPATH':f'{project}/src:{project}'}
            result=subprocess.run([sys.executable,'-m','frontend.cli','--backtest',str(config)],cwd=project,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(output.exists())
            self.assertIn('invalid_request',result.stderr)
            self.assertNotIn('Traceback',result.stderr)
