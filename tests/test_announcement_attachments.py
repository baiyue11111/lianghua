import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from protocal import build_data


class AttachmentCaptureTests(unittest.TestCase):
    def capture(self, plan, fetcher):
        if importlib.util.find_spec('collect.documents') is None:
            self.fail('uniform attachment capture is not implemented')
        from collect.documents import capture
        return capture(plan, self.data, fetcher=fetcher)

    def plan(self):
        return {'origin': 'https://static.cninfo.com.cn/', 'index_complete': False,
                'entries': [{'relative_path': 'finalpage/2020-01-01/a.PDF'},
                            {'relative_path': 'finalpage/2020-01-01/b.html'}]}

    def test_all_formats_captured_and_offline_replay_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data = build_data(Path(tmp))
            root = self.data.path("sources", "announcement_documents", "manifest.json").parent
            def fetch(url):
                return (200, {'Content-Type': 'application/octet-stream'},
                        b'%PDF-test' if url.endswith('a.PDF') else b'<html>text</html>')
            report = self.capture(self.plan(), fetch)
            self.assertEqual(report['attempted'], 2)
            self.assertTrue(report['transport_complete'])
            self.assertEqual(report['release_status'], 'NOT_RELEASED')
            before = (root/'manifest.json').read_bytes()
            def forbidden(url):
                self.fail('replay must not fetch')
            self.capture(self.plan(), forbidden)
            self.assertEqual(before, (root/'manifest.json').read_bytes())
            self.assertEqual(sorted(p.read_bytes() for p in root.glob('*.raw')),
                             [b'%PDF-test', b'<html>text</html>'])

    def test_failed_http_body_retained_and_next_attachment_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data = build_data(Path(tmp))
            root = self.data.path("sources", "announcement_documents", "manifest.json").parent
            def fetch(url):
                return (404 if url.endswith('a.PDF') else 200, {}, b'original body')
            report = self.capture(self.plan(), fetch)
            self.assertEqual(report['attempted'], 2)
            self.assertEqual(report['failures'], 1)
            self.assertFalse(report['transport_complete'])
            self.assertEqual(len(list(root.glob('*.raw'))), 2)

    def test_transport_failure_retained_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data = build_data(Path(tmp))
            root = self.data.path("sources", "announcement_documents", "manifest.json").parent
            def fetch(url):
                raise OSError('connection unavailable')
            report = self.capture(self.plan(), fetch)
            self.assertEqual(report['failures'], 2)
            def forbidden(url):
                self.fail('cached failures must not retry')
            self.assertEqual(self.capture(self.plan(), forbidden), report)

    def test_corrupted_raw_rejected_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data = build_data(Path(tmp))
            root = self.data.path("sources", "announcement_documents", "manifest.json").parent
            self.capture(self.plan(), lambda url: (200, {}, b'original'))
            before = (root/'manifest.json').read_bytes()
            next(root.glob('*.raw')).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                self.capture(self.plan(), lambda url: self.fail('no refresh'))
            self.assertEqual((root/'manifest.json').read_bytes(), before)

    def test_changed_receipt_rejected_before_manifest_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.data = build_data(Path(tmp))
            root = self.data.path("sources", "announcement_documents", "manifest.json").parent
            self.capture(self.plan(), lambda url: (200, {}, b'original'))
            before = (root/'manifest.json').read_bytes()
            source = json.loads(before)['sources'][0]
            receipt = root/source['receipt_file']
            value = json.loads(receipt.read_bytes()); value['http_status'] = 404
            receipt.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'receipt hash mismatch'):
                self.capture(self.plan(), lambda url: self.fail('no refresh'))
            self.assertEqual((root/'manifest.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
