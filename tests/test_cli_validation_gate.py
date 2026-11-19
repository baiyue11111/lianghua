import asyncio
from contextlib import chdir, closing, contextmanager, redirect_stderr, redirect_stdout
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from frontend import cli


@contextmanager
def cli_fixture():
    from backend import build_backend
    from market import build_data
    from tests.history_fixture import make_snapshot

    ui = import_module("frontend.ui-protocol")
    with TemporaryDirectory() as folder:
        root = Path(folder)
        make_snapshot(root)
        with closing(build_data(root)) as data:
            data.update_registry(lambda registry: {**registry, "datasets": [{"id": "fixture"}]})
            with closing(build_backend(root)) as backend, chdir(root), patch.object(
                ui, "_send_backend", side_effect=lambda request: asyncio.run(backend.send(request)),
            ):
                yield root, backend, data


def run_cli(config):
    output, errors = StringIO(), StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = cli.main(["--backtest", str(config)])
    return code, output.getvalue(), errors.getvalue()


class CliValidationGateTests(unittest.TestCase):
    def test_config_decoder_rejects_nonstandard_json_numbers(self):
        from protocol import _decode_backtest_config

        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                _decode_backtest_config(b'{"market":{"filter":' + token + b"}}")

    def test_real_cli_preserves_decimal_capital_from_numeric_json(self):
        with cli_fixture() as (root, _, _):
            config = root / "config.json"
            config.write_text(
                '{"module":"plugins.strategies.moving_average_cross",'
                '"params":{"fast_window":1,"slow_window":2},"dataset_id":"fixture",'
                '"intervals":[{"start":"2020-01-31","end":"2020-01-31"}],'
                '"initial_cash":9007199254740993.10}'
            )
            code, output, errors = run_cli(config)
            self.assertEqual(code, 0, errors)
            result = json.loads(output)["intervals"][0]["result"]
            self.assertEqual(result["effective_config"]["initial_cash"], "9007199254740993.10")

    def test_real_cli_runs_ordered_independent_intervals_with_exact_capital(self):
        ui = import_module("frontend.ui-protocol")
        with cli_fixture() as (root, backend, _):
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
            code, output, errors = run_cli(config)
            self.assertEqual(code, 0, errors)
            self.assertEqual(errors, "")
            batch = json.loads(output)
            self.assertEqual(batch["run_id"], "interval-batch")
            intervals = batch["intervals"]
            self.assertEqual([item["index"] for item in intervals], [1, 2, 3])
            self.assertEqual([item["name"] for item in intervals], ["甲", "甲", None])
            self.assertEqual([item["run_id"] for item in intervals], [
                "interval-batch-interval-0001",
                "interval-batch-interval-0002",
                "interval-batch-interval-0003",
            ])
            effective = [item["result"]["effective_config"] for item in intervals]
            self.assertEqual([(item["start"], item["end"]) for item in effective], [
                ("2020-02-04T00:00:00+00:00", "2020-02-04T23:59:59.999999+00:00"),
                ("2020-01-31T00:00:00+00:00", "2020-02-03T23:59:59.999999+00:00"),
                ("2020-02-03T00:00:00+00:00", "2020-02-04T23:59:59.999999+00:00"),
            ])
            self.assertEqual([item["initial_cash"] for item in effective], ["1000000"] * 3)
            self.assertEqual([item["result"]["metrics"]["initial_equity"] for item in intervals],
                             [1000000] * 3)
            for item in intervals:
                message, = asyncio.run(backend.send(ui.UiRequest(
                    f"run-query-{item['index']}", "research.run",
                    "urn:lianghua:ui:input:research.run:v1", {"id": item["run_id"]},
                )))
                self.assertEqual(message.kind, "resource")
                self.assertEqual(message.data["id"], item["run_id"])
                self.assertEqual(message.data["manifest_file"], "lake:runs/" + item["run_id"])

    def test_real_cli_rejects_invalid_backtest_configs_with_clear_errors(self):
        with cli_fixture() as (root, _, _):
            cases = {
                "missing.json": (None, "module_failed"),
                "invalid.json": ("{", "Expecting property name"),
                "wrapped.json": ('{"request": {}}', "request wrapper"),
                "legacy-range.json": (
                    '{"module":"x","start":"2020-01-01","end":"2020-01-02"}', "intervals",
                ),
                "unknown.json": (
                    '{"module":"x","intervals":[{"start":"2020-01-01","end":"2020-01-02"}],"unexpected":1}',
                    "unsupported request fields",
                ),
            }
            for name, (content, needle) in cases.items():
                with self.subTest(name=name):
                    config = root / name
                    if content is not None:
                        config.write_text(content)
                    code, output, errors = run_cli(config)
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertIn(needle, errors)
                    self.assertNotIn("未连接", errors)
                    self.assertNotIn("Traceback", errors)

    def test_missing_dataset_is_reported_without_a_successful_run(self):
        from backend import Research

        with cli_fixture() as (root, _, data):
            config = root / "config.json"
            config.write_text(json.dumps({
                "run_id": "missing-input",
                "module": "plugins.strategies.moving_average_cross",
                "params": {"fast_window": 1, "slow_window": 2},
                "initial_cash": 100000,
                "dataset_id": "missing-dataset",
                "intervals": [{"start": "2020-01-31", "end": "2020-01-31"}],
            }))
            code, output, errors = run_cli(config)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertIn("market scope is not registered for this dataset", errors)
            self.assertNotIn("Traceback", errors)
            research = Research(data)
            self.assertEqual(research.rows("runs"), [])
            failure, = research.rows("run_failures", "missing-input")
            self.assertEqual(failure["code"], "interval_failed")


if __name__ == "__main__":
    unittest.main()
