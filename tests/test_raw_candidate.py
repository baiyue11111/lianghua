import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from raw_candidate import build_candidate


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class RawCandidateTests(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder); daily = root/'daily'; evidence = root/'evidence'
        daily.mkdir(); evidence.mkdir()
        bar = dict(code='600111',date='2025-01-02',open=100.,high=110.,low=90.,close=100.,volume=100.,amount=1000.,tradestatus=1.,name='unchanged',close_hfq=200.)
        pq.write_table(pa.Table.from_pylist([bar,{**bar,'code':'600222'}]),daily/'2025.parquet')
        request = dict(code='sh.600111',start_date='2025-01-02',end_date='2025-01-02',fields='date,code,open,high,low,close,volume,amount,tradestatus',method='query_history_k_data_plus',frequency='d',adjustflag='3',affected_rows=1)
        raw = dict(error_code='0',error_msg='success',fields=request['fields'].split(','),rows=[['2025-01-02','sh.600111','10','11','9','10','100','1000','1']])
        payload = dict(schema=1,request=request,request_sha256=digest(request),raw_response=raw,raw_response_sha256=digest(raw),validation_status='VERIFIED_RAW_RESPONSE',rejection_reason=None)
        target = evidence/(digest(request)+'.json'); target.write_text(json.dumps(payload))
        (evidence/'manifest.json').write_text(json.dumps(dict(schema=1,lake_sha256={'2025.parquet':hashlib.sha256((daily/'2025.parquet').read_bytes()).hexdigest()},requests=[request])))
        return daily,evidence,target

    def test_changes_are_audited_and_source_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            daily,evidence,_ = self.fixture(folder)
            original = (daily/'2025.parquet').read_bytes()
            out = Path(folder)/'candidate'
            manifest = build_candidate(daily,evidence,out)
            rows = pq.read_table(out/'daily'/'2025.parquet').to_pylist()
            self.assertEqual(len(rows),2)
            self.assertEqual([r['close'] for r in rows],[10.,100.])
            self.assertIsNone(rows[0]['close_hfq'])
            self.assertEqual(rows[1]['close_hfq'],200.)
            self.assertEqual(rows[0]['name'],'unchanged')
            self.assertEqual((daily/'2025.parquet').read_bytes(),original)
            changes = pq.read_table(out/'changes.parquet').to_pylist()
            close = next(r for r in changes if r['field']=='close')
            self.assertEqual((close['before'],close['after']),(100.,10.))
            self.assertEqual(manifest['release_status'],'NOT_RELEASED')
            second = build_candidate(daily,evidence,Path(folder)/'repeat')
            self.assertEqual(manifest,second)

    def test_corrupt_response_cannot_repair_data(self):
        with tempfile.TemporaryDirectory() as folder:
            daily,evidence,target = self.fixture(folder)
            payload = json.loads(target.read_text()); payload['raw_response']['rows'][0][5]='999'
            target.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                build_candidate(daily,evidence,Path(folder)/'candidate')

    def test_verified_provider_only_date_is_added_and_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            daily,evidence,target=self.fixture(folder)
            payload=json.loads(target.read_text())
            payload['request']['end_date']='2025-01-03'
            payload['request_sha256']=digest(payload['request'])
            payload['raw_response']['rows'].append(['2025-01-03','sh.600111','11','12','10','11','100','1100','1'])
            payload['raw_response_sha256']=digest(payload['raw_response'])
            target.unlink()
            (evidence/(payload['request_sha256']+'.json')).write_text(json.dumps(payload))
            manifest=json.loads((evidence/'manifest.json').read_text())
            manifest['requests']=[payload['request']]
            (evidence/'manifest.json').write_text(json.dumps(manifest))
            output=Path(folder)/'candidate'
            result=build_candidate(daily,evidence,output)
            self.assertEqual(result['counts']['added_rows'],1)
            rows=pq.read_table(output/'daily'/'2025.parquet').to_pylist()
            self.assertEqual(len(rows),3)
            added=next(r for r in rows if r['date']=='2025-01-03')
            self.assertEqual(added['close'],11.)
            self.assertIsNone(added['close_hfq'])
            self.assertEqual(pq.read_table(output/'added.parquet').num_rows,1)


if __name__=='__main__': unittest.main()
