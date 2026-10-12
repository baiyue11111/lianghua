"""Uniform raw attachment capture. No parsing, strategy access or source fallback.

Transport completion is not index completeness, document validity, or release.
All stored failures are replayed without retry; corrupt evidence stops the run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from .data import saved


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fetch_attachment(url):
    import requests
    response = requests.get(url, headers={'Referer': 'https://www.cninfo.com.cn/'},
                            timeout=20, allow_redirects=False)
    return response.status_code, dict(response.headers), response.content


def capture(plan, data, identity="announcement_documents", *, fetcher=fetch_attachment):
    origin = plan['origin']
    if origin != 'https://static.cninfo.com.cn/':
        raise ValueError('official attachment origin required')
    paths = [entry['relative_path'] for entry in plan['entries']]
    if not paths or paths != sorted(set(paths)):
        raise ValueError('nonempty unique sorted attachment paths required')
    for path in paths:
        if not isinstance(path, str) or not path.startswith('finalpage/') or '..' in path.split('/') or any(c in path for c in ('?', '#', '\\')):
            raise ValueError('invalid attachment path')
    report = {'schema': 1, 'plan_sha256': sha(encoded(plan)),
              'collector_sha256': sha(Path(__file__).read_bytes()),
              'index_complete': plan['index_complete'], 'attempted': 0,
              'failures': 0, 'transport_complete': False,
              'release_status': 'NOT_RELEASED', 'sources': []}
    manifest_bytes = saved(data, 'sources', identity, 'manifest.json')
    if manifest_bytes is not None:
        previous = json.loads(manifest_bytes)
        if previous['plan_sha256'] != report['plan_sha256']:
            raise ValueError('capture plan changed; separate evidence directory required')
        for source in previous['sources']:
            receipt_bytes = data.read('sources', identity, source['receipt_file'])
            if sha(receipt_bytes) != source['receipt_sha256']:
                raise ValueError('receipt hash mismatch')
            receipt = json.loads(receipt_bytes)
            if 'raw_sha256' in receipt and sha(data.read('sources', identity, receipt['raw_file'])) != receipt['raw_sha256']:
                raise ValueError('raw hash mismatch')
    try:
        for path in paths:
            url = origin + path
            request_identity = {'url': url, 'method': 'GET',
                        'request_headers': {'Referer': 'https://www.cninfo.com.cn/'}}
            key = sha(encoded(request_identity)); receipt_name = key + '.json'
            raw_name = key + '.raw'
            receipt_bytes = saved(data, 'sources', identity, receipt_name)
            if receipt_bytes is not None:
                receipt = json.loads(receipt_bytes)
                if receipt['request'] != request_identity:
                    raise ValueError('request identity mismatch')
                if 'raw_sha256' in receipt and sha(data.read('sources', identity, raw_name)) != receipt['raw_sha256']:
                    raise ValueError('raw hash mismatch')
            else:
                if saved(data, 'sources', identity, raw_name) is not None:
                    raise ValueError('unreceipted raw evidence; no refresh')
                receipt = {'schema': 1, 'request': request_identity}
                try:
                    status, headers, raw = fetcher(url)
                except Exception as exc:
                    receipt.update(error_type=type(exc).__name__, error=str(exc))
                else:
                    data.write('sources', identity, raw_name, raw)
                    receipt.update(http_status=status, response_headers=headers,
                                   raw_file=raw_name, raw_sha256=sha(raw), raw_bytes=len(raw))
                data.write('sources', identity, receipt_name, encoded(receipt) + b'\n')
            success = receipt.get('http_status') == 200
            report['attempted'] += 1
            report['failures'] += int(not success)
            report['sources'].append({'receipt_file': receipt_name,
                                      'receipt_sha256': sha(data.read('sources', identity, receipt_name)),
                                      'http_200': success})
            print(f"{report['attempted']}/{len(paths)} {path}: "
                  f"{receipt.get('http_status', receipt.get('error_type'))}", flush=True)
        report['transport_complete'] = report['failures'] == 0
    finally:
        report['run_id'] = sha(encoded(report))
        data.write('sources', identity, 'manifest.json', encoded(report) + b'\n')
    return report
