from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest

from market import DataBus, FACT_DECIMAL, FACT_NUMERIC, build_data
from market.ingest import declared_pattern


class IngestionTests(unittest.TestCase):
    def test_declared_pattern_pins_manifest_dates(self) -> None:
        template = 'source/facts/adjust_{symbol}_{start}_{end}.json'
        spec = {'start': '1990-01-01', 'end': '2026-10-13', 'template': template}
        pattern = declared_pattern(template, spec)
        self.assertEqual(pattern, 'source/facts/adjust_*_1990-01-01_2026-10-13.json')
        body = pattern.rsplit('/', 1)[-1]
        regex = re.compile(''.join('[^/]*' if character == '*' else re.escape(character)
                                   for character in body) + r'\Z')
        names = ['adjust_sh.600601_1990-01-01_2026-10-13.json',
                 'adjust_sh.600601_1990-01-01_2026-10-19.json',
                 'adjust_sh.600601_1990-01-01_2026-09-18.json']
        self.assertEqual([name for name in names if regex.match(name)], names[:1])
        self.assertEqual(declared_pattern('source/facts/dividend_{symbol}_{year}.json', {}),
                         'source/facts/dividend_*_*.json')
        self.assertEqual(declared_pattern('source/facts/stock_basic_{symbol}.json', {}),
                         'source/facts/stock_basic_*.json')

    def test_fact_decimal_keeps_long_source_values(self) -> None:
        with TemporaryDirectory() as directory:
            data = build_data(Path(directory))
            try:
                values = {'forward': '0.0001882683548866871',
                          'backward': '0.0038023348608514224'}
                rows, types = DataBus._fact_rows(
                    [('fact:adjust:sh.600601:1990-01-01:2026-10-13:', [values])],
                    FACT_NUMERIC['adjust'])
                self.assertEqual(types['forward'], FACT_DECIMAL)
                data.put_rows('fact_adjust', rows, keys=('reference', 'position'),
                              types=types, storage='parquet')
                saved = data.table('lake:fact_adjust').to_pylist()[0]
                self.assertEqual(saved['forward'], Decimal('0.0001882683548866871'))
                self.assertEqual(saved['backward'], Decimal('0.0038023348608514224'))
            finally:
                data.close()


if __name__ == "__main__":
    unittest.main()
