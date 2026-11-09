"""The daily publish path delivers rows; the store owns merge, types and atomicity."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market import build_data
from protocol import DataError


class DailyPublishTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.data = build_data(Path(self._tmp.name))
        from collect.methods import Methods
        self.methods = Methods(self.data)

    def tearDown(self):
        self.data.close()
        self._tmp.cleanup()

    @staticmethod
    def bar(code, day, close):
        return {'code': code, 'date': day, 'open': close, 'high': close, 'low': close,
                'close': close, 'volume': 100.0, 'amount': 1000.0}

    def publish(self, rows, *, dry_run=False):
        return self.methods._publish_daily(
            'src', None, {'records': rows, 'request': {}}, dry_run=dry_run)

    def stored(self):
        return sorted(self.data.table(self.data.source('src', 'bars')).to_pylist(),
                      key=lambda row: (row['code'], row['date']))

    def test_publish_never_reads_the_bulk_table(self):
        with patch.object(type(self.methods), '_source_rows',
                          side_effect=AssertionError('publish must not read the bulk table')):
            self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        self.assertEqual(len(self.stored()), 1)

    def test_new_date_appends_and_stored_rows_survive(self):
        self.publish([self.bar('sh.600000', '2026-10-30', 9.0)])
        self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        rows = self.stored()
        self.assertEqual([row['date'] for row in rows], ['2026-10-30', '2026-11-09'])
        self.assertEqual([row['close'] for row in rows], [9.0, 10.0])

    def test_a_stored_bar_wins_over_a_later_differing_value(self):
        # Re-requesting a range resends bars already held; the stored bar is the
        # record of what was observed, so the overlap is skipped rather than
        # refused. One revised bar must not fail the codes sharing its batch.
        self.publish([self.bar('sh.600000', '2026-10-30', 9.0)])
        result = self.publish([self.bar('sh.600000', '2026-10-30', 9.5),
                               self.bar('sz.000001', '2026-11-09', 20.0)])
        self.assertEqual(result['failures'], [])
        self.assertEqual([(row['code'], row['date'], row['close']) for row in self.stored()],
                         [('sh.600000', '2026-10-30', 9.0), ('sz.000001', '2026-11-09', 20.0)])

    def test_date_keeps_the_type_the_catalog_declared(self):
        self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        name = self.data.source('src', 'bars')[5:]
        self.assertEqual(self.data.definition(name).columns['date'], 'VARCHAR')

    def test_first_write_declares_the_full_shape(self):
        self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        name = self.data.source('src', 'bars')[5:]
        self.assertEqual(dict(self.data.definition(name).columns), {
            'code': 'VARCHAR', 'date': 'VARCHAR', 'open': 'DOUBLE', 'high': 'DOUBLE',
            'low': 'DOUBLE', 'close': 'DOUBLE', 'volume': 'DOUBLE', 'amount': 'DOUBLE',
            'price_basis': 'VARCHAR'})

    def test_new_column_widens_the_registered_shape(self):
        self.publish([self.bar('sh.600000', '2026-10-30', 9.0)])
        self.publish([{**self.bar('sh.600000', '2026-11-09', 10.0), 'turn': 1.5}])
        name = self.data.source('src', 'bars')[5:]
        self.assertEqual(self.data.definition(name).columns['turn'], 'DOUBLE')
        self.assertEqual([row.get('turn') for row in self.stored()], [None, 1.5])

    def test_a_created_source_is_append_only(self):
        self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        name = self.data.source('src', 'bars')[5:]
        self.assertEqual(tuple(self.data.definition(name).updates), ())

    def test_refresh_is_refused_instead_of_silently_rewriting(self):
        self.publish([self.bar('sh.600000', '2026-10-30', 9.0)])
        with self.assertRaises(DataError) as caught:
            self.methods._publish_daily('src', None, {
                'records': [self.bar('sh.600000', '2026-10-30', 9.5)], 'request': {'refresh': True}})
        self.assertEqual(caught.exception.code, 'unsupported_refresh')
        self.assertEqual([row['close'] for row in self.stored()], [9.0])

    def test_dry_run_reports_without_writing(self):
        result = self.publish([self.bar('sh.600000', '2026-11-09', 10.0)], dry_run=True)
        self.assertEqual(result, {'incoming': 1, 'records': 1, 'failures': []})
        with self.assertRaises(DataError):
            self.data.source('src', 'bars')

    def test_manifest_digest_tracks_the_file_set(self):
        self.publish([self.bar('sh.600000', '2026-10-30', 9.0)])
        reference = self.data.source('src', 'bars')
        first = self.data.manifest_digest(reference)
        self.assertEqual(self.data.manifest_digest(reference), first)
        self.publish([self.bar('sh.600000', '2026-11-09', 10.0)])
        self.assertNotEqual(self.data.manifest_digest(reference), first)


if __name__ == '__main__':
    unittest.main()
