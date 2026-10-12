from pathlib import Path
import tempfile
import unittest
import json
import pyarrow as pa
import pyarrow.parquet as pq

from tools import price_gate_audit


class PriceGateAuditTests(unittest.TestCase):
    def test_actual_loader_errors_and_master_gaps_are_retained_in_repeatable_readonly_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); daily=root/'daily'; cache=root/'cache'; basic=root/'basic'
            for folder in (daily,cache,basic): folder.mkdir()
            pq.write_table(pa.Table.from_pylist([{'code':'600000','date':'2020-01-02','name':'A',
                'open':10.,'high':11.,'low':9.,'close':-10.,'volume':1000.,'amount':10000.,'tradestatus':1.}]),
                daily/'2020.parquet')
            master=root/'master.parquet'
            pq.write_table(pa.table({'code':['600000','920000']}),master)
            changes=root/'changes.json'; changes.write_text('{"schema":1,"events":[]}')
            args=['--daily',str(daily),'--cache',str(cache),'--stock-basic',str(basic),
                  '--master',str(master),'--code-changes',str(changes),
                  '--factor-start','1990-01-01','--factor-end','2026-09-18']
            self.assertTrue(hasattr(price_gate_audit,'main'),'missing reproducible full-scope price audit')
            first=root/'one.json'; second=root/'two.json'
            self.assertEqual(price_gate_audit.main([*args,'--output',str(first)]),1)
            self.assertEqual(price_gate_audit.main([*args,'--output',str(second)]),1)
            self.assertEqual(first.read_bytes(),second.read_bytes())
            report=json.loads(first.read_text())
            self.assertEqual(report['raw_rows_checked'],1)
            self.assertEqual(report['missing_master_histories'],['920000'])
            self.assertEqual({i['reason'] for i in report['issues']},{'missing_factor_cache','invalid_lake_price'})
            self.assertFalse(report['price_gate_passed'])
            self.assertFalse(report['backtest_started'])
            self.assertEqual(report['release_status'],'NOT_RELEASED')
            self.assertTrue(report['inputs_unchanged'])
            self.assertEqual(len(list(daily.iterdir())),1)
