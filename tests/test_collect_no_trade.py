"""Historical state evidence remains readable without coupling ordinary collection to it."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from collect.methods import Methods
from market import build_data, table_name
from protocol import Filter, TableQuery
from tests.history_fixture import make_snapshot


class NoTradeTests(unittest.TestCase):
    def test_bound_evidence_closes_only_the_sessions_bars_do_not_cover(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_snapshot(root, symbols=('600003',))
            bus = build_data(root)
            try:
                methods = Methods(bus, {})
                bars = bus.source('fixture', 'bars')[5:]
                bus.delete(TableQuery(bars, (Filter('code', 'eq', 'sh.600003'),
                                             Filter('date', 'eq', '2020-02-03'))))
                query = lambda: methods.daily_coverage(
                    'fixture', ('sh.600003',), date(2020, 1, 31), date(2020, 2, 3), ('close',))
                self.assertFalse(query()['complete'])

                name = table_name('fixture/no_trade')
                bus.put_rows(name, [{'code': 'sh.600003', 'start': '2020-01-31',
                                     'end': '2020-02-03'}], keys=('code', 'start', 'end'))
                bus.bind('fixture', 'no_trade', 'lake:' + name)
                self.assertTrue(query()['complete'])
            finally:
                bus.close()
