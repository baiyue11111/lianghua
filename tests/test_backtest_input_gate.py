"""Exercise the actual legacy loader with real tiny Parquet inputs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tools import run_backtest


class BacktestInputGateTests(unittest.TestCase):
    def test_legacy_factor_ratio_ledger_cannot_generate_another_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backtest'
            with patch.object(run_backtest, 'OUTDIR', output):
                with self.assertRaisesRegex(RuntimeError, 'cash dividends'):
                    run_backtest.main(['--prefix', 'invalid'])
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(RuntimeError, 'cash dividends'):
            run_backtest.run({}, {}, object())

    def load(self, factor, price=10.0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pq.write_table(pa.table({
                'symbol': ['000001'], 'name': ['fixture'],
                'open': [[price]], 'close': [[price]], 'amount': [[100000000.0]],
                'adj_factor': [[factor]],
            }), root / 'panel.parquet')
            pq.write_table(pa.table({'trade_date': ['2020-01-02']}), root / 'calendar.parquet')
            benchmark = root / 'benchmark.json'
            benchmark.write_text('[{"trade_date":"2020-01-02","close":100}]')
            with patch.object(run_backtest, 'CLEAN', root), patch.object(run_backtest, 'BENCH', benchmark):
                return run_backtest.load_panel()

    def test_valid_price_requires_explicit_positive_finite_factor(self):
        for factor in (float('nan'), float('inf'), 0.0, -1.0):
            with self.subTest(factor=factor), self.assertRaisesRegex(ValueError, 'adjustment'):
                self.load(factor)

    def test_explicit_factor_one_is_preserved(self):
        self.assertEqual(self.load(1.0)['adj'][0, 0], 1.0)

    def test_no_price_gap_is_not_filled_with_factor_one(self):
        self.assertTrue(np.isnan(self.load(float('nan'), float('nan'))['adj'][0, 0]))
