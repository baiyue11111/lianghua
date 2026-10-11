"""Validate captured provider responses against their exact request and hashes."""
from hashlib import sha256
from strategy import canonical_hash


def validate_cache(payload:dict,request:dict)->None:
    if payload.get('schema')!=2 or payload.get('request')!=request:
        raise ValueError('provider cache request/schema mismatch')
    if payload.get('request_sha256')!=canonical_hash(request):
        raise ValueError('provider cache request hash mismatch')
    raw=payload.get('raw_response')
    if not isinstance(raw,dict) or payload.get('raw_response_sha256')!=canonical_hash(raw):
        raise ValueError('provider cache response hash mismatch')
    fields,rows=raw.get('fields'),raw.get('rows')
    if (not isinstance(fields,list) or len(set(fields))!=len(fields) or not isinstance(rows,list)
            or 'code' not in fields or raw.get('code')!=request['code'] or str(raw.get('error_code'))!='0'):
        raise ValueError('provider cache fields/status/code invalid')
    if payload.get('request_fields_sha256')!=sha256(','.join(fields).encode()).hexdigest():
        raise ValueError('provider cache field hash mismatch')
    if any(not isinstance(row,list) or len(row)!=len(fields) or row[fields.index('code')]!=request['code'] for row in rows):
        raise ValueError('provider cache source row invalid')
    if payload.get('status')!=('ok' if rows else 'empty'):
        raise ValueError('provider cache status and rows disagree')
