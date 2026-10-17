import subprocess
import sys
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class CliValidationGateTests(unittest.TestCase):
    def test_config_decoder_rejects_nonstandard_json_numbers(self):
        from protocal import _decode_backtest_config

        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                _decode_backtest_config(b'{"market":{"filter":' + token + b"}}")

    def test_real_cli_preserves_decimal_capital_from_numeric_json(self):
        import json
        from tests.history_fixture import make_snapshot
        from protocal import build_data

        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            make_snapshot(root)
            data = build_data(root)
            registry_path = root / "data/source/registry/sources.json"
            registry = json.loads(registry_path.read_text())
            registry.update(sources=[], datasets=[{"id": "fixture"}], benchmarks=[])
            data.write("sources", "registry", "sources.json", json.dumps(registry).encode())
            config = root / "config.json"
            config.write_text(
                '{"module":"plugins.strategies.moving_average_cross",'
                '"params":{"fast_window":1,"slow_window":2},"dataset_id":"fixture",'
                '"intervals":[{"start":"2020-01-31","end":"2020-01-31"}],'
                '"initial_cash":9007199254740993.10}'
            )
            env = {**os.environ, "PYTHONPATH": f"{project}/src:{project}"}
            result = subprocess.run([
                sys.executable, "-m", "frontend.cli", "--backtest", str(config),
            ], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["intervals"][0]["result"]["effective_config"]["initial_cash"],
                             "9007199254740993.10")

    def test_real_cli_runs_ordered_independent_intervals_with_exact_capital(self):
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
            registry_path = root / "data/source/registry/sources.json"
            registry = json.loads(registry_path.read_text())
            registry.update(sources=[], datasets=[{"id": "fixture"}], benchmarks=[])
            data.write("sources", "registry", "sources.json", json.dumps(registry).encode())
            config = root / "config.json"
            config.write_text(json.dumps({
                "run_id": "interval-batch",
                "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "intervals": [
                    {"name": "甲", "start": "2020-02-04", "end": "2020-02-04"},
                    {"name": "甲", "start": "2020-01-31", "end": "2020-02-03"},
                    {"start": "2020-02-03", "end": "2020-02-04"},
                ],
                "initial_cash": 1000000,
            }))
            env = {**os.environ, "PYTHONPATH": f"{project}/src:{project}"}
            result = subprocess.run([
                sys.executable, "-m", "frontend.cli", "--backtest", str(config),
            ], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            output = json.loads(result.stdout)
            self.assertEqual(output["run_id"], "interval-batch")
            self.assertEqual([item["index"] for item in output["intervals"]], [1, 2, 3])
            self.assertEqual([item["name"] for item in output["intervals"]], ["甲", "甲", None])
            self.assertEqual([item["run_id"] for item in output["intervals"]], [
                "interval-batch-interval-0001", "interval-batch-interval-0002",
                "interval-batch-interval-0003",
            ])
            self.assertEqual([
                (item["result"]["effective_config"]["start"],
                 item["result"]["effective_config"]["end"])
                for item in output["intervals"]
            ], [
                ("2020-02-04T00:00:00+00:00", "2020-02-04T23:59:59.999999+00:00"),
                ("2020-01-31T00:00:00+00:00", "2020-02-03T23:59:59.999999+00:00"),
                ("2020-02-03T00:00:00+00:00", "2020-02-04T23:59:59.999999+00:00"),
            ])
            self.assertTrue(all(item["result"]["effective_config"]["initial_cash"] == "1000000"
                                for item in output["intervals"]))
            self.assertNotIn("9007199254740992", result.stdout)
            self.assertTrue((root / "data" / "result").exists())
            backend = build_backend(root)
            try:
                messages = [asyncio.run(backend.send(ui.UiRequest(
                    f"run-query-{item['index']}", "workbench.run",
                    "urn:lianghua:ui:input:workbench.run:v1", {"id": item["run_id"]},
                )))[0] for item in output["intervals"]]
            finally:
                backend.close()
            self.assertTrue(all(message.kind == "resource" for message in messages))
            self.assertEqual([message.data["id"] for message in messages], [
                item["run_id"] for item in output["intervals"]
            ])

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
            (root/'unknown.json').write_text(
                '{"module":"x","intervals":[{"start":"2020-01-01","end":"2020-01-02"}],"unexpected":1}',
                encoding='utf-8')
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
            self.assertIn('intervals',result.stderr)
            self.assertNotIn('Traceback',result.stderr)
