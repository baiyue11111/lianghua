import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.corporate_action_candidate import load_records, build_candidate
from collect.factors import _sha256


class CorporateActionCandidateTests(unittest.TestCase):
    def fixture(self, folder):
        root=Path(folder); cache=root/'cache'; reviews=root/'reviews'
        cache.mkdir(); reviews.mkdir()
        request=dict(kind='dividend',code='sz.001323',start=None,end=None,year='2025')
        row=dict(code='sz.001323',dividRegistDate='2025-08-20',dividOperateDate='2025-08-21',
                 dividPayDate='',dividStockMarketDate='2025-08-21',dividCashPsBeforeTax='1',
                 dividStocksPs='0',dividReserveToStockPs='')
        raw=dict(code='sz.001323',error_code='0',error_msg='success',fields=list(row),rows=[list(row.values())])
        payload=dict(schema=2,status='ok',request=request,request_sha256=_sha256(request),
                     raw_response=raw,raw_response_sha256=_sha256(raw),
                     request_fields_sha256=hashlib.sha256(','.join(raw['fields']).encode()).hexdigest())
        path=cache/'dividend_001323_2025.json'; path.write_text(json.dumps(payload))
        proof=reviews/'proof.pdf'; proof.write_bytes(b'%PDF-1.4\nfixture')
        review=dict(schema=1,code='001323',operation_date='2025-08-21',
                    raw_provider_file=str(path),raw_provider_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    governing_source=dict(file='proof.pdf',sha256=hashlib.sha256(proof.read_bytes()).hexdigest()),
                    corrected_fields=[dict(field='dividCashPsBeforeTax',before='1',after='0'),
                                      dict(field='dividReserveToStockPs',before='',after='0.1')],
                    stock_credit_date='2025-08-21',stock_tradable_from='2025-08-21')
        (reviews/'event_review.json').write_text(json.dumps(review))
        return root,cache,reviews,path,proof

    def test_source_review_changes_terms_without_modifying_original(self):
        with TemporaryDirectory() as folder:
            root,cache,reviews,path,_=self.fixture(folder)
            before=path.read_bytes()
            records,audit=load_records(path,reviews)
            self.assertEqual(records[0]['dividCashPsBeforeTax'],'0')
            self.assertEqual(records[0]['dividReserveToStockPs'],'0.1')
            self.assertEqual(records[0]['stock_credit_date'],'2025-08-21')
            self.assertEqual(len(audit['changes']),2)
            self.assertEqual(path.read_bytes(),before)
            first=build_candidate(cache,reviews,root/'first')
            self.assertIn('factors.py',first['code_sha256'])
            second=build_candidate(cache,reviews,root/'second')
            self.assertEqual(first,second)
            events=json.loads((root/'first'/'events.json').read_text())
            self.assertEqual(events[0]['cash_per_share'],0)
            self.assertEqual(events[0]['reserve_to_stock_per_share'],0.1)
            self.assertEqual(first['release_status'],'NOT_RELEASED')

    def test_modified_pdf_cannot_authorize_correction(self):
        with TemporaryDirectory() as folder:
            _,_,reviews,path,proof=self.fixture(folder)
            proof.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'source hash'):
                load_records(path,reviews)

    def test_explicit_plan_review_preserves_rows_and_applies_final_terms(self):
        with TemporaryDirectory() as folder:
            root,cache,reviews,path,proof=self.fixture(folder)
            empty=root/'empty'; empty.mkdir()
            j=json.loads(path.read_text()); raw=j['raw_response']
            cash=raw['fields'].index('dividCashPsBeforeTax')
            raw['rows'][0][cash]='0.201529'
            raw['rows'].append(list(raw['rows'][0])); raw['rows'][1][cash]='0.2'
            j['raw_response_sha256']=_sha256(raw); path.write_text(json.dumps(j))
            before=path.read_bytes()
            resolution=reviews/'resolutions.json'
            resolution.write_text(json.dumps(dict(schema=1,reviews=[dict(
                provider_file=path.name,provider_sha256=hashlib.sha256(before).hexdigest(),
                document=proof.name,document_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),
                rows=[dict(row=i,distribution_id='annual-2024',before_cash_per_share=value,
                           implemented_cash_per_share='0.2015287')
                      for i,value in enumerate(('0.201529','0.2'))])])) )
            result=build_candidate(cache,empty,root/'candidate',resolutions=resolution)
            events=json.loads((root/'candidate'/'events.json').read_text())
            self.assertEqual(len(events),2)
            self.assertEqual([e['distribution_id'] for e in events],['annual-2024','annual-2024'])
            self.assertEqual([e['cash_per_share'] for e in events],[0.2015287,0.2015287])
            self.assertEqual(result['counts']['field_changes'],2)
            self.assertEqual(result['counts']['reviewed_events'],2)
            self.assertEqual(result['counts']['plan_identified_observations'],2)
            self.assertEqual(path.read_bytes(),before)
            proof.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'source hash'):
                build_candidate(cache,empty,root/'tampered',resolutions=resolution)

    def test_unparseable_record_remains_in_rejection_artifact(self):
        with TemporaryDirectory() as folder:
            root,cache,reviews,path,_=self.fixture(folder)
            # No review applies in this independent invalid-source case.
            empty=root/'empty'; empty.mkdir()
            j=json.loads(path.read_text()); j['raw_response']['rows'][0][2]='bad-date'
            j['raw_response_sha256']=_sha256(j['raw_response']); path.write_text(json.dumps(j))
            result=build_candidate(cache,empty,root/'candidate')
            self.assertEqual(result['counts']['source_rows'],1)
            self.assertEqual(result['counts']['normalized_events'],0)
            self.assertEqual(result['counts']['rejected_rows'],1)
            rejected=json.loads((root/'candidate'/'rejected.json').read_text())
            self.assertEqual(rejected[0]['record']['dividOperateDate'],'bad-date')


if __name__=='__main__': unittest.main()
