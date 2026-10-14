from pathlib import Path
from hashlib import sha256
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
        for name in ['data/source/input/daily', 'data/source/input/cache']:(root/name).mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([
            {'code':'600001','date':day,'name':'x','close':value}
            for day,value in [('2023-01-03',1.),('2023-01-04',2.),('2023-01-05',3.)]]+
            [{'code':'600002','date':'2023-01-03','name':'y','close':9.}]),root/'data/source/input/daily/bars.parquet')
        (root/'data/source/input/calendar.json').write_text(json.dumps({'provider':'BaoStock','request':{'start':'2023-01-01','end':'2023-01-06'},'response':{
            'error_code':'0','fields':['calendar_date','is_trading_day'],
            'rows':[['2023-01-01','0'],['2023-01-02','0'],['2023-01-03','1'],['2023-01-04','1'],['2023-01-05','1'],['2023-01-06','1']]}}))
        (root/'data/source/input/changes.json').write_text('{"schema":1,"events":[]}')
        (root/'data/source/input/cache/adjust_600001_1990-01-01_2023-01-06.json').write_text('{}')
        (root/'data/source/input/cache/dividend_600001_2023.json').write_text(json.dumps({'raw_response':{'fields':[],'rows':[]}}))
        policy=root/'data/source/input/account_policy.json';policy.write_text('{"sources":[]}')
        def ref(path):
            value=path.read_bytes()
            return {'kind':'sources','identity':'input','name':path.relative_to(root/'data/source/input').as_posix(),
                    'file':path.relative_to(root).as_posix(),'sha256':sha256(value).hexdigest(),'size':len(value)}
        return {'daily_dir':'data/source/input/daily','cache_dir':'data/source/input/cache','calendar_source':'data/source/input/calendar.json','code_changes':'data/source/input/changes.json',
            'account_policy_ref':ref(policy),'dataset_id':'out','symbols':['600001'],'fields':{},'universe':'fixed_symbols','history_start':'2023-01-03','end':'2023-01-05','factor_start':'1990-01-01','factor_end':'2023-01-06'}

    def test_reads_only_requested_symbols_dates_and_records_next_session(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root);prepare(root,config)
            rows=pq.read_table(root/'data/source/out/daily/bars.parquet').to_pylist()
            self.assertEqual([r['code'] for r in rows],['600001']*3)
            self.assertEqual([r['close'] for r in rows],[1.,2.,3.])
            calendar=json.loads((root/'data/source/out/calendar.json').read_text())
            self.assertEqual(calendar['sessions'],['2023-01-03','2023-01-04','2023-01-05','2023-01-06'])
            manifest=json.loads((root/'data/source/out/manifest.json').read_text())
            self.assertNotIn('account_scenarios',manifest)
            self.assertEqual(manifest['scope']['symbols'],['600001'])

    def test_missing_price_session_does_not_create_a_successful_manifest(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root)
            rows=pq.read_table(root/'data/source/input/daily/bars.parquet').to_pylist()
            pq.write_table(pa.Table.from_pylist([r for r in rows if r['date']!='2023-01-04']),root/'data/source/input/daily/bars.parquet')
            with self.assertRaises(ValueError):prepare(root,config)
            self.assertFalse((root/'data/source/out/manifest.json').exists())

    def test_tampered_explicit_source_fails_before_publish(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root)
            policy=root/'data/source/input/account_policy.json'
            policy.write_text('{"sources":[],"tampered":true}')
            with self.assertRaisesRegex(ValueError,'account policy source hash mismatch'):
                prepare(root,config)
            self.assertFalse((root/'data/source/out/manifest.json').exists())

    def test_missing_explicit_source_keeps_existing_manifest(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root);prepare(root,config)
            manifest=root/'data/source/out/manifest.json'
            before=manifest.read_bytes()
            config['account_policy_ref']={**config['account_policy_ref'],
                'identity':'missing','name':'account_policy.json',
                'file':'data/source/missing/account_policy.json','size':0,
                'sha256':'0'*64}
            with self.assertRaisesRegex(ValueError,'account policy source is missing'):
                prepare(root,config)
            self.assertEqual(manifest.read_bytes(),before)

    def test_policy_source_cannot_cycle_through_output_identity(self):
        prepare=self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=self.fixture(root)
            config['account_policy_ref']={**config['account_policy_ref'],
                'identity':config['dataset_id'],
                'file':f"data/source/{config['dataset_id']}/account_policy.json"}
            with self.assertRaisesRegex(ValueError,'independent from dataset output'):
                prepare(root,config)
            self.assertFalse((root/'data/source/out').exists())
