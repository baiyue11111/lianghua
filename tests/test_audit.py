from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
import math
from datetime import date, timedelta

from audit import deterministic_run_id, write_audit_bundle
from research import BacktestResult


class AuditTests(unittest.TestCase):
    def test_independent_output_directories_have_identical_audit_data(self):
        result = BacktestResult('fixture', ((date(2020, 1, 1), 100000.0),
                                          (date(2020, 1, 2), 100100.0)), (), (), (), ())
        with TemporaryDirectory() as directory:
            roots = [Path(directory) / 'first', Path(directory) / 'second']
            runs = [write_audit_bundle(result=result, source_hashes={'fixture': 'input'},
                config_hash='config', research_tier='exploratory', output_root=root,
                limitations=['SYNTHETIC_TEST']) for root in roots]
            self.assertEqual(runs[0].name, runs[1].name)
            files = [sorted(p.relative_to(run) for p in run.rglob('*') if p.is_file()) for run in runs]
            self.assertEqual(files[0], files[1])
            for relative in files[0]:
                with self.subTest(file=relative):
                    if relative.name == 'artifact.json':
                        artifacts = [json.loads((run / relative).read_text()) for run in runs]
                        for artifact in artifacts:
                            artifact['manifest'].pop('generatedAt')
                            artifact['snapshot'].pop('generatedAt')
                        self.assertEqual(artifacts[0], artifacts[1])
                    else:
                        self.assertEqual((runs[0] / relative).read_bytes(), (runs[1] / relative).read_bytes())

    def test_existing_run_is_not_overwritten(self):
        result = BacktestResult('fixture', ((date(2020, 1, 1), 100000.0),
                                          (date(2020, 1, 2), 100100.0)), (), (), (), ())
        with TemporaryDirectory() as directory:
            kwargs = dict(result=result, source_hashes={'fixture': 'input'}, config_hash='config',
                          research_tier='exploratory', output_root=Path(directory), limitations=[])
            run = write_audit_bundle(**kwargs)
            before = {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}
            with self.assertRaises(FileExistsError):
                write_audit_bundle(**kwargs)
            after = {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_high_sharpe_without_validation_cannot_become_qualified(self):
        equity = []
        value = 100000.0
        for index in range(400):
            value *= 1 + 0.001 + 0.0002 * math.sin(index)
            equity.append((date(2020, 1, 1) + timedelta(days=index), value))
        result = BacktestResult('synthetic', tuple(equity), (), (), (), ())
        with TemporaryDirectory() as directory:
            run = write_audit_bundle(result=result, source_hashes={'fixture': 'synthetic'},
                config_hash='fixture', research_tier='exploratory', output_root=Path(directory),
                limitations=['UNVERIFIED_ADJUSTMENT', 'DIVIDEND_TAX_MISSING'])
            manifest = json.loads((run / 'manifest.json').read_text())
            metrics = json.loads((run / 'metrics.json').read_text())
            self.assertGreater(metrics['sharpe'], 2)
            self.assertEqual(manifest['status'], 'UNVALIDATED_BACKTEST')

    def test_run_id_changes_only_when_reproducibility_inputs_change(self) -> None:
        first = deterministic_run_id({"data": "a"}, "config", "code")
        self.assertEqual(first, deterministic_run_id({"data": "a"}, "config", "code"))
        self.assertNotEqual(first, deterministic_run_id({"data": "b"}, "config", "code"))


if __name__ == "__main__":
    unittest.main()
