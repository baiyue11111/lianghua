"""Read-only coverage audit; does not invent pre-event adjustment anchors."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb

from collect.methods import Request, _validate_cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    args = parser.parse_args()
    files = sorted(args.daily.glob('*.parquet'))
    if not files:
        raise ValueError('no daily partitions')
    con = duckdb.connect()
    bars = con.read_parquet([str(p) for p in files])
    bounds = bars.aggregate('code, min(date) first_date, max(date) last_date', 'code').order('code').fetchall()
    result = {'schema': 1, 'stocks': len(bounds), 'missing': [], 'errors': [],
              'missing_initial_anchor': [], 'inputs': [],
              'interpretation': 'Cache integrity and date coverage only; not financial correctness or source completeness.'}
    for p in files:
        with p.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        result['inputs'].append({'path': str(p), 'sha256': digest})
    for code, first, last in bounds:
        request = Request('adjust', code, args.start, args.end, None)
        path = args.cache / (request.key + '.json')
        if not path.exists():
            result['missing'].append(code)
            continue
        raw = path.read_bytes()
        result['inputs'].append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
        try:
            payload = json.loads(raw)
            _validate_cache(request, payload)
            response = payload['raw_response']
            events = [dict(zip(response['fields'], row)) for row in response['rows']]
            dates = [event['dividOperateDate'] for event in events]
            if not dates or dates != sorted(set(dates)):
                raise ValueError('empty, duplicate or unordered events')
            if dates[0] > str(first):
                result['missing_initial_anchor'].append({
                    'code': code, 'first_bar': str(first), 'last_bar': str(last),
                    'first_factor_event': events[0]})
        except (ValueError, KeyError, TypeError) as exc:
            result['errors'].append({'code': code, 'error': str(exc)})
    result['initial_anchor_date_gate_passed'] = not any(result[key] for key in (
        'missing', 'errors', 'missing_initial_anchor'))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
