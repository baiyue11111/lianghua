from pathlib import Path
import importlib.util
import json
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq


class PrepareHistoryTests(unittest.TestCase):
    def builder(self):
        path=Path(__file__).parents[1]/'tools/prepare_history.py'
        self.assertTrue(path.exists(),'source preparation must be executable')
        spec=importlib.util.spec_from_file_location('prepare_history',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.prepare

    def fixture(self,root):
        for name in ['daily','cache','data/sources/out/sources']:(root/name).mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([
            {'code':'600001','date':day,'name':'x','close':value}
            for day,value in [('2023-01-03',1.),('2023-01-04',2.),('2023-01-05',3.)]]+
            [{'code':'600002','date':'2023-01-03','name':'y','close':9.}]),root/'daily/bars.parquet')
        (root/'calendar.json').write_text(json.dumps({'provider':'BaoStock','request':{'start':'2023-01-01','end':'2023-01-06'},'response':{
            'error_code':'0','fields':['calendar_date','is_trading_day'],
            'rows':[['2023-01-01','0'],['2023-01-02','0'],['2023-01-03','1'],['2023-01-04','1'],['2023-01-05','1'],['2023-01-06','1']]}}))
        (root/'changes.json').write_text('{"schema":1,"events":[]}')
        (root/'cache/adjust_600001_1990-01-01_2023-01-06.json').write_text('{}')
        (root/'cache/dividend_600001_2023.json').write_text(json.dumps({'raw_response':{'fields':[],'rows':[]}}))
        (root/'data/sources/out/account_policy.json').write_text('{"sources":[]}')
        return {'daily_dir':'daily','cache_dir':'cache','calendar_source':'calendar.json','code_changes':'changes.json',
            'dataset_id':'out','symbols':['600001'],'fields':{},'universe':'fixed_symbols','history_start':'2023-01-03','end':'2023-01-05','factor_start':'1990-01-01','factor_end':'2023-01-06'}

    def test_reads_only_requested_symbols_dates_and_records_next_session(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root);prepare(root,config)
            rows=pq.read_table(root/'data/sources/out/daily/bars.parquet').to_pylist()
            self.assertEqual([r['code'] for r in rows],['600001']*3)
            self.assertEqual([r['close'] for r in rows],[1.,2.,3.])
            calendar=json.loads((root/'data/sources/out/calendar.json').read_text())
            self.assertEqual(calendar['sessions'],['2023-01-03','2023-01-04','2023-01-05','2023-01-06'])
            manifest=json.loads((root/'data/sources/out/manifest.json').read_text())
            self.assertNotIn('account_scenarios',manifest)
            self.assertEqual(manifest['scope']['symbols'],['600001'])

    def test_missing_price_session_does_not_create_a_successful_manifest(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root)
            rows=pq.read_table(root/'daily/bars.parquet').to_pylist()
            pq.write_table(pa.Table.from_pylist([r for r in rows if r['date']!='2023-01-04']),root/'daily/bars.parquet')
            with self.assertRaises(ValueError):prepare(root,config)
            self.assertFalse((root/'data/sources/out/manifest.json').exists())
