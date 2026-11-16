"""Adjusted reads use committed per-security evidence, not a global watermark."""
from datetime import date

import numpy as np

from tests.test_adjusted_panel import AdjustedPanelTests, _bar, _sha256_json, FACTOR_START
from core.calculation import AdjustmentCoverageError
from market import fact_ref
from market.reader import _load_adjusted_panel


class FactorCoverageReadTests(AdjustedPanelTests):
    def save_adjustment(self, code, end, back=1.0, start=None, event='2020-01-02'):
        rows = [{'date': event, 'forward': 1.0, 'backward': back}]
        return self.data.save_fact({'schema': 1, 'kind': 'adjust', 'code': code,
            'start': start or FACTOR_START.isoformat(), 'end': end, 'year': None,
            'source_hash': _sha256_json(rows), 'records': rows})

    def read_at(self, end, **kwargs):
        with self.data.transaction(readonly=True):
            return _load_adjusted_panel(self.data, 'fixture', factor_start=FACTOR_START,
                factor_end=date.fromisoformat(end), code_changes_path=self.code_changes,
                factor_template=fact_ref('adjust', '{symbol}', FACTOR_START, end).replace('%7Bsymbol%7D', '{symbol}'),
                stock_basic_template=fact_ref('security', '{symbol}').replace('%7Bsymbol%7D', '{symbol}'),
                read=self.data.object, **kwargs)

    def test_new_security_coverage_is_read_before_global_watermark_advances(self):
        self.write_bars([_bar('2020-01-02', close=10, open_=10, high=11, low=9),
                         _bar('2020-01-06', close=11, open_=11, high=12, low=10)])
        reference = self.save_adjustment('sh.600000', '2020-01-06')
        panel = self.read_at('2020-01-03')
        np.testing.assert_allclose(panel.signal_close[0], [10, 11])
        self.assertIn(reference, [item.path for item in panel.source_hashes])

    def test_missing_security_is_reported_without_borrowing_another_coverage(self):
        self.write_bars([_bar('2020-01-06', close=10, open_=10, high=11, low=9),
                         _bar('2020-01-06', close=10, open_=10, high=11, low=9, code='600001')])
        self.save_adjustment('sh.600000', '2020-01-06')
        self.save_adjustment('sh.600001', '2020-01-03')
        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.read_at('2020-01-03')
        self.assertIn('600001', {issue.code for issue in caught.exception.issues})
        self.assertNotIn('600000', {issue.code for issue in caught.exception.issues})

    def test_as_of_rejects_future_snapshot(self):
        self.write_bars([_bar('2020-01-06', close=10, open_=10, high=11, low=9)])
        self.save_adjustment('sh.600000', '2020-01-07')
        with self.assertRaises(AdjustmentCoverageError):
            self.read_at('2020-01-03', as_of=date(2020, 1, 6))

    def test_conflicting_overlapping_factor_facts_are_rejected(self):
        self.write_bars([_bar('2020-01-06', close=10, open_=10, high=11, low=9)])
        self.save_adjustment('sh.600000', '2020-01-06', back=1.0)
        self.save_adjustment('sh.600000', '2020-01-07', back=2.0)
        with self.assertRaises(AdjustmentCoverageError):
            self.read_at('2020-01-03')

    def test_conflict_in_partially_overlapping_fact_is_rejected(self):
        self.write_bars([_bar('2020-01-02', close=10, open_=10, high=11, low=9),
                         _bar('2020-01-06', close=11, open_=11, high=12, low=10)])
        self.save_adjustment('sh.600000', '2020-01-05', back=2.0)
        self.save_adjustment('sh.600000', '2020-01-07', back=1.0)
        with self.assertRaises(AdjustmentCoverageError):
            self.read_at('2020-01-03')

    def test_rename_uses_separate_old_and_new_security_windows(self):
        self.write_bars([_bar('2020-01-01', close=10, open_=10, high=11, low=9),
                         _bar('2020-01-02', close=11, open_=11, high=12, low=10)])
        old = self.save_adjustment('sh.600001', '2020-01-01', back=2, event='2020-01-01')
        new = self.save_adjustment('sh.600000', '2020-01-02', back=2, start='2020-01-02')
        self.write_changes({'schema': 1, 'events': [{'old_code': '600001', 'new_code': '600000',
            'effective_date': '2020-01-02', 'factor_basis_continuity_verified': True}]})
        panel = self.read_at('2020-01-03')
        np.testing.assert_allclose(panel.back_factor[0], [2, 2])
        self.assertTrue({old, new} <= {item.path for item in panel.source_hashes})
        panel = self.read_at('2020-01-03', as_of=date(2020, 1, 2))
        np.testing.assert_allclose(panel.back_factor[0], [2, 2])

    def test_rename_with_original_codes_does_not_require_old_end_at_as_of(self):
        self.write_bars([_bar('2020-01-01', close=10, open_=10, high=11, low=9, code='600001'),
                         _bar('2020-01-02', close=11, open_=11, high=12, low=10)])
        self.save_adjustment('sh.600001', '2020-01-01', back=2, event='2020-01-01')
        self.save_adjustment('sh.600000', '2020-01-02', back=2, start='2020-01-02')
        self.write_changes({'schema': 1, 'events': [{'old_code': '600001', 'new_code': '600000',
            'effective_date': '2020-01-02', 'factor_basis_continuity_verified': True}]})
        panel = self.read_at('2020-01-03', as_of=date(2020, 1, 2))
        self.assertEqual(set(panel.symbols), {'600000', '600001'})
