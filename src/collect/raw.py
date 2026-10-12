"""Capture raw provider evidence for lake anomalies; never overwrite lake data."""
from __future__ import annotations

import json
from .data import saved

from raw_bar_evidence import discrepancy_requests, validate_response
from collect.factors import _sha256
from collect.quotes import raw_response


def investigate(data, identity="raw_investigation", timeout=30) -> None:
    daily = data.path('sources', 'lake', 'daily/_.parquet').parent
    requests = discrepancy_requests(daily)
    source_hashes = {ref.name: ref.sha256 for ref in data.list('sources', 'lake')
                     if ref.name.startswith('daily/') and ref.name.endswith('.parquet')}
    manifest = {'schema':1,'lake_sha256':source_hashes,'requests':requests}
    previous = saved(data, 'sources', identity, 'manifest.json')
    if previous is not None and json.loads(previous) != manifest:
        raise ValueError('investigation source changed: choose a new data identity')
    data.write('sources', identity, 'manifest.json', (json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode())
    for request in requests:
        key = _sha256(request)
        name = f'{key}.json'
        captured = saved(data, 'sources', identity, name)
        if captured is not None:
            payload = json.loads(captured)
            if payload['request'] != request or payload['raw_response_sha256'] != _sha256(payload['raw_response']):
                raise ValueError('evidence hash mismatch')
            print(f"retained {request['code']} {payload['validation_status']}",flush=True)
            continue
        raw = raw_response(request, timeout)
        fields, rows = raw['fields'], raw['rows']
        status, reason = 'VERIFIED_RAW_RESPONSE', None
        try:
            validate_response(request,fields,rows)
        except ValueError as exc:
            status,reason = 'REJECTED_RAW_RESPONSE',str(exc)
        payload = {'schema':1,'request':request,'request_sha256':key,
                   'raw_response':raw,'raw_response_sha256':_sha256(raw),
                   'validation_status':status,'rejection_reason':reason}
        data.write('sources', identity, name, (json.dumps(payload,ensure_ascii=False,indent=2)+'\n').encode())
        print(f"{request['code']} {len(rows)} rows {status}: {reason or ''}",flush=True)
