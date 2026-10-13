import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class DocumentTextCandidateTests(unittest.TestCase):
    def build(self, inventory, source, output):
        if importlib.util.find_spec('tools.document_text_candidate') is None:
            self.fail('audited text batch is not implemented')
        from tools.document_text_candidate import build_candidate
        return build_candidate(inventory, source, output)

    def fixture(self, root):
        source = root/'source'; source.mkdir()
        rows = []
        for number, content_type in enumerate(('text/html', 'text/html; charset=UTF-8')):
            raw = b'<meta charset="gb2312"><p>2020-01-02</p>'
            name = f'{number}.raw'; (source/name).write_bytes(raw)
            receipt = {'request': {'url': f'https://example.test/{number}'},
                       'http_status': 200, 'response_headers': {'Content-Type': content_type},
                       'raw_file': name, 'raw_sha256': hashlib.sha256(raw).hexdigest()}
            path = source/f'{number}.json'; path.write_text(json.dumps(receipt))
            rows.append({'receipt_file': path.name, 'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        inventory = root/'inventory.json'
        inventory.write_text(json.dumps({'schema': 1, 'index_complete': False,
                                        'transport_complete': False, 'sources': rows}))
        return inventory, source

    def test_all_sources_processed_conflicts_preserved_repeat_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inventory, source = self.fixture(root)
            report = self.build(inventory, source, root/'a')
            self.assertEqual(report['counts'], {'documents': 2, 'extracted': 1, 'incomplete': 0, 'rejected': 1})
            documents = json.loads((root/'a'/'documents.json').read_bytes())
            self.assertEqual(documents[0]['text']['sections'], ['2020-01-02\n'])
            self.assertIn('conflicting encoding', documents[1]['error'])
            self.assertEqual(report['release_status'], 'NOT_RELEASED')
            self.build(inventory, source, root/'b')
            for name in ('documents.json', 'manifest.json'):
                self.assertEqual((root/'a'/name).read_bytes(), (root/'b'/name).read_bytes())

    def test_raw_tamper_stops_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inventory, source = self.fixture(root)
            (source/'0.raw').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'raw hash mismatch'):
                self.build(inventory, source, root/'a')
            self.assertFalse((root/'a').exists())

    def test_existing_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inventory, source = self.fixture(root)
            self.build(inventory, source, root/'a')
            with self.assertRaisesRegex(ValueError, 'new output'):
                self.build(inventory, source, root/'a')

    def test_http_success_without_raw_evidence_never_reuses_previous_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inventory, source = self.fixture(root)
            path = source/'1.json'; receipt = json.loads(path.read_bytes())
            receipt.pop('raw_file'); receipt.pop('raw_sha256')
            receipt['response_headers']['Content-Type'] = 'text/html'
            path.write_text(json.dumps(receipt))
            payload = json.loads(inventory.read_bytes())
            payload['sources'][1]['receipt_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'successful response requires raw evidence'):
                self.build(inventory, source, root/'a')
            self.assertFalse((root/'a').exists())


if __name__ == '__main__':
    unittest.main()
