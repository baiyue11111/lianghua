"""Raw document bytes → immutable source text. No network, files or event inference."""
from dataclasses import dataclass
import codecs
from html.parser import HTMLParser
import io
import re


@dataclass(frozen=True)
class DocumentText:
    media_type: str
    encoding: str | None
    sections: tuple[str, ...]
    issues: tuple[str, ...]


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden.append(tag)
        elif not self.hidden and tag == 'br':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            if not self.hidden or self.hidden.pop() != tag:
                raise ValueError('unbalanced hidden HTML tags')
        elif not self.hidden and tag in ('p', 'pre', 'td', 'div', 'tr', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_document(raw: bytes, content_type: str) -> DocumentText:
    """Reject conflicting/missing encoding and media; do not guess or repair.

    PDF sections preserve page boundaries. Empty sections remain auditable issues;
    successfully extracted text is not proof of layout fidelity or event validity.
    """
    if not raw:
        raise ValueError('empty document')
    media = content_type.split(';', 1)[0].strip().lower()
    if media == 'application/pdf':
        if not raw.startswith(b'%PDF-'):
            raise ValueError('PDF signature mismatch')
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.xref_index != 0:
            raise ValueError('PDF cross-reference correction required; source rejected')
        sections = tuple(page.extract_text() for page in reader.pages)
        encoding = None
    elif media in ('text/html', 'text/plain'):
        declarations = re.findall(r'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', content_type, re.I)
        if media == 'text/html':
            for meta in re.findall(rb'<meta\b[^>]*>', raw, re.I):
                declarations.extend(value.decode('ascii') for value in
                                    re.findall(rb'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', meta, re.I))
        encodings = {codecs.lookup(value).name for value in declarations}
        if not encodings:
            raise ValueError('declared encoding required')
        if len(encodings) != 1:
            raise ValueError('conflicting encoding declarations')
        encoding = encodings.pop()
        text = raw.decode(encoding, errors='strict')
        if media == 'text/html':
            parser = _HTMLText(); parser.feed(text); parser.close()
            if parser.hidden:
                raise ValueError('unclosed hidden HTML tags')
            text = ''.join(parser.parts)
        sections = (text,)
    else:
        raise ValueError(f'unsupported media type: {media}')
    issues = tuple(f'EMPTY_TEXT_SECTION:{number}' for number, text in enumerate(sections, 1)
                   if not text.strip())
    if not sections:
        issues = ('NO_TEXT_SECTIONS',)
    return DocumentText(media, encoding, sections, issues)
