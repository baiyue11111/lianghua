import json
from pathlib import Path
import tempfile
import unittest

from collect.methods import discrepancy_requests, validate_response


class RawBarEvidenceTests(unittest.TestCase):
    def test_requests_come_from_anomalies_not_stock_allowlist(self):
        rows = [
            {'code': '600111', 'date': '2025-01-02', 'open': 10, 'high': 11, 'low': 9,
             'close': 10, 'volume': 100, 'amount': 1000, 'tradestatus': 1},
            {'code': '600222', 'date': '2025-01-02', 'open': 1, 'high': 1.1, 'low': .9,
             'close': 1, 'volume': 100, 'amount': 1000, 'tradestatus': 1},
            {'code': '600222', 'date': '2025-01-03', 'open': 1, 'high': 1.1, 'low': .9,
             'close': 1, 'volume': 100, 'amount': 1000, 'tradestatus': 1},
            {'code': '300555', 'date': '2025-01-03', 'open': 10, 'high': 10, 'low': 10,
             'close': 10, 'volume': 0, 'amount': 0, 'tradestatus': None},
        ]
        result = discrepancy_requests(rows)
        self.assertEqual([(r['code'],r['start_date'],r['end_date']) for r in result],
                         [('sh.600222','2025-01-02','2025-01-03'),
                          ('sz.300555','2025-01-03','2025-01-03')])
        self.assertEqual([r['affected_rows'] for r in result], [2, 1])

    def test_wrong_code_and_duplicate_provider_rows_fail(self):
        request = {'code':'sh.600111','start_date':'2025-01-02','end_date':'2025-01-03'}
        fields = ['date','code','open','high','low','close','volume','amount','tradestatus']
        good = ['2025-01-02','sh.600111','10','11','9','10','100','1000','1']
        self.assertEqual(validate_response(request, fields, [good]), 1)
        with self.assertRaises(ValueError):
            validate_response(request, fields, [good,good])
        wrong = good.copy(); wrong[1] = 'sh.600222'
        with self.assertRaises(ValueError):
            validate_response(request, fields, [wrong])

    def test_bad_raw_basis_and_empty_are_not_accepted(self):
        request = {'code':'sh.600111','start_date':'2025-01-02','end_date':'2025-01-03'}
        fields = ['date','code','open','high','low','close','volume','amount','tradestatus']
        with self.assertRaises(ValueError):
            validate_response(request, fields, [])
        with self.assertRaises(ValueError):
            validate_response(request, fields, [['2025-01-02','sh.600111','1','1','1','1','100','1000','1']])
        self.assertEqual(validate_response(request, fields, [['2025-01-02','sh.600111','10','10','10','10','0','0','0']]), 1)


if __name__ == '__main__':
    unittest.main()
