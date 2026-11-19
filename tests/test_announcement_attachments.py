from contextlib import closing
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from market import build_data
from protocol import Filter, TableQuery


PREFIX = "collect:announcement_documents:"


class AttachmentCaptureTests(unittest.TestCase):
    def manifest(self, data):
        return data.config(PREFIX + "manifest")

    def receipt(self, data, source):
        return data.config(PREFIX + source["receipt_file"])

    def capture(self, data, plan, fetcher):
        from collect.methods import capture_attachments
        return capture_attachments(plan, data, fetcher=fetcher)

    def plan(self):
        return {'origin': 'https://static.cninfo.com.cn/', 'index_complete': False,
                'entries': [{'relative_path': 'finalpage/2020-01-01/a.PDF'},
                            {'relative_path': 'finalpage/2020-01-01/b.html'}]}

    def test_all_formats_captured_and_offline_replay_identical(self):
        with tempfile.TemporaryDirectory() as tmp, closing(build_data(Path(tmp))) as data:
            def fetch(url):
                return (200, {'Content-Type': 'application/octet-stream'},
                        b'%PDF-test' if url.endswith('a.PDF') else b'<html>text</html>')
            report = self.capture(data, self.plan(), fetch)
            self.assertEqual(report['attempted'], 2)
            self.assertTrue(report['transport_complete'])
            self.assertEqual(report['release_status'], 'NOT_RELEASED')
            before = self.manifest(data)
            def forbidden(url):
                self.fail('replay must not fetch')
            self.capture(data, self.plan(), forbidden)
            self.assertEqual(before, self.manifest(data))
            self.assertEqual(sorted(row["content"] for row in data.scan(TableQuery("attachments")).table.to_pylist()),
                             [b'%PDF-test', b'<html>text</html>'])

    def test_failed_http_body_retained_and_next_attachment_attempted(self):
        with tempfile.TemporaryDirectory() as tmp, closing(build_data(Path(tmp))) as data:
            def fetch(url):
                return (404 if url.endswith('a.PDF') else 200, {}, b'original body')
            report = self.capture(data, self.plan(), fetch)
            self.assertEqual(report['attempted'], 2)
            self.assertEqual(report['failures'], 1)
            self.assertFalse(report['transport_complete'])
            self.assertEqual(data.scan(TableQuery('attachments')).table.num_rows, 2)

    def test_transport_failure_retained_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp, closing(build_data(Path(tmp))) as data:
            def fetch(url):
                raise OSError('connection unavailable')
            report = self.capture(data, self.plan(), fetch)
            self.assertEqual(report['failures'], 2)
            def forbidden(url):
                self.fail('cached failures must not retry')
            self.assertEqual(self.capture(data, self.plan(), forbidden), report)

    def test_corrupted_raw_rejected_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, closing(build_data(Path(tmp))) as data:
            self.capture(data, self.plan(), lambda url: (200, {}, b'original'))
            before = self.manifest(data)
            receipt = self.receipt(data, before["sources"][0])
            identity = sha256(("announcement_documents/" + receipt["raw_file"]).encode()).hexdigest()
            query = TableQuery("attachments", (Filter("id", "eq", identity),))
            row, = data.scan(query).table.to_pylist()
            data.delete(query)
            data.put_rows("attachments", [{**row, "content": b"changed"}], keys=("id",))
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                self.capture(data, self.plan(), lambda url: self.fail('no refresh'))
            self.assertEqual(self.manifest(data), before)

    def test_changed_receipt_rejected_before_manifest_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp, closing(build_data(Path(tmp))) as data:
            self.capture(data, self.plan(), lambda url: (200, {}, b'original'))
            before = self.manifest(data)
            source = before["sources"][0]
            value = self.receipt(data, source)
            data.save_config(PREFIX + source["receipt_file"], {**value, "http_status": 404})
            with self.assertRaisesRegex(ValueError, 'receipt hash mismatch'):
                self.capture(data, self.plan(), lambda url: self.fail('no refresh'))
            self.assertEqual(self.manifest(data), before)


if __name__ == '__main__':
    unittest.main()
