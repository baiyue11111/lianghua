"""Build an immutable source-backed text candidate; never infer corporate actions."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform

from document_text import extract_document


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_file(root, name):
    if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
        raise ValueError('unsafe source filename')
    return root/name


def build_candidate(inventory, source, output):
    inventory, source, output = map(Path, (inventory, source, output))
    if output.exists():
        raise ValueError('new output directory required')
    payload = json.loads(inventory.read_bytes())
    if payload['schema'] != 1 or not payload['sources']:
        raise ValueError('nonempty source inventory schema 1 required')
    source_hashes = {str(inventory): digest(inventory)}
    rows = []; seen = set()
    for item in payload['sources']:
        path = local_file(source, item['receipt_file'])
        if path.name in seen:
            raise ValueError('duplicate source receipt')
        seen.add(path.name)
        if digest(path) != item['receipt_sha256']:
            raise ValueError('receipt hash mismatch')
        source_hashes[str(path)] = item['receipt_sha256']
        receipt = json.loads(path.read_bytes())
        if receipt.get('http_status') == 200 and not {'raw_file', 'raw_sha256'} <= receipt.keys():
            raise ValueError('successful response requires raw evidence')
        row = {'source_receipt': path.name, 'receipt_sha256': item['receipt_sha256'],
               'url': receipt['request']['url'], 'status': 'REJECTED'}
        if 'raw_file' in receipt:
            raw_path = local_file(source, receipt['raw_file'])
            if digest(raw_path) != receipt['raw_sha256']:
                raise ValueError('raw hash mismatch')
            source_hashes[str(raw_path)] = receipt['raw_sha256']
            row.update(raw_file=raw_path.name, raw_sha256=receipt['raw_sha256'])
        if receipt.get('http_status') != 200:
            row['error'] = f"source HTTP/transport failure: {receipt.get('http_status', receipt.get('error_type'))}"
        else:
            headers = {k.lower(): v for k, v in receipt['response_headers'].items()}
            row['content_type'] = headers.get('content-type', '')
            try:
                text = extract_document(raw_path.read_bytes(), row['content_type'])
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=str(exc))
            else:
                row.update(text=asdict(text), status='INCOMPLETE_TEXT' if text.issues else 'TEXT_EXTRACTED')
        rows.append(row)
    # Reject concurrent mutation before publishing any output.
    for path, expected in source_hashes.items():
        if digest(path) != expected:
            raise ValueError('source changed during extraction')
    import pypdf
    documents = encoded(rows) + b'\n'
    report = {'schema': 1, 'release_status': 'NOT_RELEASED', 'event_terms_validated': False,
              'index_complete': payload['index_complete'],
              'transport_complete': payload['transport_complete'],
              'source_files_sha256': source_hashes,
              'code_sha256': {str(path): digest(path) for path in
                              (Path(__file__), Path(extract_document.__code__.co_filename))},
              'runtime': {'python': platform.python_version(), 'pypdf': pypdf.__version__},
              'counts': {'documents': len(rows),
                         'extracted': sum(r['status'] == 'TEXT_EXTRACTED' for r in rows),
                         'incomplete': sum(r['status'] == 'INCOMPLETE_TEXT' for r in rows),
                         'rejected': sum(r['status'] == 'REJECTED' for r in rows)},
              'documents_sha256': hashlib.sha256(documents).hexdigest()}
    report['run_id'] = hashlib.sha256(encoded(report)).hexdigest()
    output.mkdir(parents=True)
    (output/'documents.json').write_bytes(documents)
    (output/'manifest.json').write_bytes(encoded(report) + b'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = build_candidate(args.inventory, args.source, args.output)
    print(json.dumps({'run_id': report['run_id'], **report['counts'],
                      'release_status': report['release_status']}, ensure_ascii=False))
    return 0 if not report['counts']['rejected'] and not report['counts']['incomplete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
