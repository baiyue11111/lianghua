import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from raw_bar_evidence import discrepancy_requests, validate_response


class RawBarEvidenceTests(unittest.TestCase):
    def test_requests_come_from_anomalies_not_stock_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            c = duckdb.connect()
            c.execute("CREATE TABLE bars(code VARCHAR,date VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,amount DOUBLE,tradestatus DOUBLE)")
            c.execute("INSERT INTO bars VALUES ('600111','2025-01-02',10,11,9,10,100,1000,1),('600222','2025-01-02',1,1.1,.9,1,100,1000,1),('600222','2025-01-03',1,1.1,.9,1,100,1000,1),('300555','2025-01-03',10,10,10,10,0,0,NULL)")
            c.execute("COPY bars TO ? (FORMAT PARQUET)", [str(Path(folder) / '2025.parquet')])
            result = discrepancy_requests(Path(folder))
            self.assertEqual([(r['code'],r['start_date'],r['end_date']) for r in result], [('sz.300555','2025-01-03','2025-01-03'),('sh.600222','2025-01-02','2025-01-03')])
            self.assertEqual([r['affected_rows'] for r in result], [1,2])
            c.close()

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
