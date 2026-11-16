"""A benchmark quotes a level, so its turnover columns are not a price.

These tests pin the split between tradable series (turnover / shares must sit
inside the daily range) and level series (an index or rate whose turnover
aggregates the market it summarises).
"""
from __future__ import annotations

import unittest

from market.reader import _BarEvidenceError, _is_level_series, _level_series_codes, _traded_bar

# A real BaoStock row for sh.000300: the level is ~4600 while the market's
# turnover per share is ~23 CNY.
LEVEL_PRICES = (4600.0, 4642.6101, 4558.7647, 4610.0)
MARKET_VOLUME = 18_543_919_600.0
MARKET_AMOUNT = 430_554_181_504.8


class LevelSeriesTests(unittest.TestCase):
    def test_tradable_series_rejects_turnover_outside_the_range(self):
        with self.assertRaises(_BarEvidenceError) as caught:
            _traded_bar(1, LEVEL_PRICES, MARKET_VOLUME, MARKET_AMOUNT)
        self.assertEqual(caught.exception.reason, 'inconsistent_price_basis')

    def test_level_series_accepts_turnover_outside_the_range(self):
        self.assertTrue(_traded_bar(1, LEVEL_PRICES, MARKET_VOLUME, MARKET_AMOUNT,
                                    level_series=True))

    def test_level_series_still_rejects_impossible_prices(self):
        with self.assertRaises(_BarEvidenceError) as caught:
            _traded_bar(1, (4600.0, 4642.0, 0.0, 4610.0), MARKET_VOLUME, MARKET_AMOUNT,
                        level_series=True)
        self.assertEqual(caught.exception.reason, 'invalid_source_price')

    def test_level_series_still_rejects_contradictory_status(self):
        with self.assertRaises(_BarEvidenceError) as caught:
            _traded_bar(0, LEVEL_PRICES, MARKET_VOLUME, MARKET_AMOUNT, level_series=True)
        self.assertEqual(caught.exception.reason, 'contradictory_trade_state')

    def test_level_codes_come_from_the_registry_benchmarks(self):
        class Data:
            @staticmethod
            def registry():
                return {'benchmarks': [
                    {'id': 'hs300', 'code': 'sh.000300'},
                    {'id': 'nasdaq_composite', 'code': 'fred.nasdaqcom'},
                    {'id': 'broken', 'code': ''},
                    {'id': 'no-code'},
                    'not-a-record',
                ]}

        codes = _level_series_codes(Data())
        self.assertIn('sh.000300', codes)
        self.assertIn('fred.nasdaqcom', codes)
        self.assertEqual(len(codes), 2)

    def test_level_codes_are_empty_when_the_registry_is_unavailable(self):
        class Data:
            @staticmethod
            def registry():
                raise RuntimeError('no registry')

        self.assertEqual(_level_series_codes(Data()), frozenset())

    def test_lookup_normalizes_the_spelling(self):
        codes = frozenset({'sh.000300'})
        self.assertTrue(_is_level_series('SH.000300', codes))
        self.assertFalse(_is_level_series('sh.600000', codes))
        self.assertFalse(_is_level_series('sh.600000', frozenset()))


if __name__ == '__main__':
    unittest.main()
