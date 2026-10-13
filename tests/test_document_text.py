import importlib.util
import unittest


class DocumentTextTests(unittest.TestCase):
    def extract(self, raw, content_type):
        if importlib.util.find_spec('document_text') is None:
            self.fail('source-aware document extraction is missing')
        from document_text import extract_document
        return extract_document(raw, content_type)

    def test_html_declared_encoding_preserves_terms_and_ignores_scripts(self):
        raw = '<meta charset="gb2312"><script>wrong date</script><style>x</style><p>股权登记日：2020年1月2日</p>'.encode('gb2312')
        result = self.extract(raw, 'text/html')
        self.assertIn('股权登记日：2020年1月2日', result.sections[0])
        self.assertNotIn('wrong date', result.sections[0])
        self.assertEqual(result.encoding, 'gb2312')
        self.assertEqual(result.issues, ())

    def test_conflicting_encoding_declarations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'conflicting encoding'):
            self.extract(b'<meta charset="gb2312"><p>ABC</p>', 'text/html; charset=UTF-8')

    def test_unknown_encoding_never_guessed(self):
        with self.assertRaisesRegex(ValueError, 'declared encoding required'):
            self.extract(b'<p>ABC</p>', 'text/html')

    def test_plain_text_strict_decode_and_empty_body(self):
        result = self.extract('现金分红'.encode(), 'text/plain; charset=utf-8')
        self.assertEqual(result.sections, ('现金分红',))
        with self.assertRaises(UnicodeDecodeError):
            self.extract(b'\xff', 'text/plain; charset=utf-8')
        with self.assertRaisesRegex(ValueError, 'empty document'):
            self.extract(b'', 'text/plain; charset=utf-8')

    def test_pdf_media_rejects_html_body(self):
        with self.assertRaisesRegex(ValueError, 'PDF signature'):
            self.extract(b'<html>error page</html>', 'application/pdf')

    def test_pdf_empty_page_is_not_complete_text(self):
        import io
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=72, height=72)
        buffer = io.BytesIO(); writer.write(buffer)
        result = self.extract(buffer.getvalue(), 'application/pdf')
        self.assertEqual(result.sections, ('',))
        self.assertEqual(result.issues, ('EMPTY_TEXT_SECTION:1',))

    def test_pdf_requiring_cross_reference_correction_is_rejected(self):
        import io
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=72, height=72)
        buffer = io.BytesIO(); writer.write(buffer)
        raw = buffer.getvalue().replace(b'xref\n0 ', b'xref\n1 ', 1)
        with self.assertRaisesRegex(ValueError, 'cross-reference correction'):
            self.extract(raw, 'application/pdf')


if __name__ == '__main__':
    unittest.main()
