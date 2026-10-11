"""Unreleased, source-backed raw-bar candidate. Original lake is never changed."""
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from raw_bar_evidence import FIELDS, validate_response
from research_registry import digest


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def build_candidate(daily: Path, evidence: Path, output: Path) -> dict:
    daily,evidence,output = map(Path,(daily,evidence,output))
    if output.exists():
        raise ValueError('candidate output must be a new directory')
    source = json.loads((evidence/'manifest.json').read_text())
    actual = {p.name:file_hash(p) for p in sorted(daily.glob('*.parquet'))}
    if source['schema'] != 1 or actual != source['lake_sha256']:
        raise ValueError('investigation lake hashes no longer match')
    patches, rejected, hashes, seen = [],[],{},set()
    for request in source['requests']:
        key = digest(request); path = evidence/(key+'.json')
        payload = json.loads(path.read_text()); raw = payload['raw_response']
        if (payload['schema'] != 1 or payload['request'] != request
                or payload['request_sha256'] != key
                or payload['raw_response_sha256'] != digest(raw)
                or request['adjustflag'] != '3' or raw['error_code'] != '0'):
            raise ValueError('raw evidence request/response integrity failed')
        hashes[path.name] = file_hash(path)
        try:
            validate_response(request,raw['fields'],raw['rows'])
        except ValueError as exc:
            if payload['validation_status'] != 'REJECTED_RAW_RESPONSE':
                raise
            rejected.append(dict(request_hash=key,code=request['code'],reason=str(exc)))
            continue
        if payload['validation_status'] != 'VERIFIED_RAW_RESPONSE':
            raise ValueError('raw evidence validation verdict disagrees')
        for values in raw['rows']:
            record = dict(zip(FIELDS.split(','),values,strict=True))
            pair = (record['code'].split('.')[1],record['date'])
            if pair in seen:
                raise ValueError('overlapping raw evidence rows')
            seen.add(pair)
            patches.append(dict(code=pair[0],date=pair[1],
                **{k:float(record[k]) if record[k] else None for k in FIELDS.split(',')[2:]},
                source_sha256=payload['raw_response_sha256']))
    if not patches:
        raise ValueError('no verified raw evidence')
    output.mkdir(); (output/'daily').mkdir()
    table = pa.Table.from_pylist(patches)
    fields = FIELDS.split(',')[2:]
    with duckdb.connect() as connection:
        connection.register('patches',table)
        connection.read_parquet([str(daily/n) for n in actual]).create_view('original')
        if connection.sql('SELECT count(*) FROM (SELECT code,date FROM original GROUP BY code,date HAVING count(*)>1)').fetchone()[0]:
            raise ValueError('duplicate original bars')
        connection.sql('SELECT p.* FROM patches p ANTI JOIN original o USING(code,date)').create_view('added')
        added_rows=connection.sql('SELECT count(*) FROM added').fetchone()[0]
        connection.execute('COPY (SELECT * FROM added ORDER BY code,date) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)',[str(output/'added.parquet')])
        schema = {row[0] for row in connection.sql('DESCRIBE original').fetchall()}
        derived = sorted(schema & {'close_hfq','float_mv','total_mv'})
        changes_sql = ' UNION ALL '.join(
            f"SELECT o.code,o.date,'{field}' field,o.\"{field}\"::DOUBLE \"before\","
            + (f'p."{field}"::DOUBLE' if field in fields else 'NULL::DOUBLE')
            + f' "after",p.source_sha256 FROM original o JOIN patches p USING(code,date) WHERE '
            + (f'o."{field}" IS DISTINCT FROM p."{field}"' if field in fields else f'o."{field}" IS NOT NULL')
            for field in fields+derived)
        connection.execute(f'COPY ({changes_sql} ORDER BY code,date,field) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)',[str(output/'changes.parquet')])
        replacements = ','.join(
            f'CASE WHEN p.code IS NOT NULL THEN p."{f}" ELSE o."{f}" END AS "{f}"' for f in fields)
        replacements += ''.join(f',CASE WHEN p.code IS NOT NULL THEN NULL ELSE o."{f}" END AS "{f}"' for f in derived)
        for name in actual:
            connection.read_parquet(str(daily/name)).create_view('partition',replace=True)
            if len(Path(name).stem)!=4 or not Path(name).stem.isdigit():
                raise ValueError('daily partitions must use YYYY.parquet names')
            connection.execute(f'COPY ((SELECT o.* REPLACE ({replacements}) FROM partition o LEFT JOIN patches p USING(code,date)) UNION ALL BY NAME (SELECT * EXCLUDE(source_sha256) FROM added WHERE substr(date,1,4)=$year) ORDER BY code,date) TO $output (FORMAT PARQUET, COMPRESSION ZSTD)',{'year':Path(name).stem,'output':str(output/'daily'/name)})
        additional_years=connection.sql('SELECT DISTINCT substr(date,1,4) FROM added ORDER BY 1').fetchall()
        for (year,) in additional_years:
            if year+'.parquet' not in actual:
                # An empty original projection preserves the full raw schema.
                connection.execute('COPY ((SELECT * FROM original WHERE FALSE) UNION ALL BY NAME (SELECT * EXCLUDE(source_sha256) FROM added WHERE substr(date,1,4)=$year) ORDER BY code,date) TO $output (FORMAT PARQUET, COMPRESSION ZSTD)',{'year':year,'output':str(output/'daily'/(year+'.parquet'))})
        counts = dict(original_rows=connection.sql('SELECT count(*) FROM original').fetchone()[0],
                      patched_rows=len(patches)-added_rows,added_rows=added_rows,
                      changed_fields=pq.read_metadata(output/'changes.parquet').num_rows)
    if actual != {p.name:file_hash(p) for p in sorted(daily.glob('*.parquet'))}:
        raise ValueError('original lake changed during candidate build')
    manifest = dict(schema=1,release_status='NOT_RELEASED',lake_sha256=actual,
        evidence_sha256=hashes,rejected_responses=rejected,counts=counts,
        recipe_sha256={p.name:file_hash(p) for p in (Path(__file__),Path(__file__).with_name('raw_bar_evidence.py'))},
        output_sha256={str(p.relative_to(output)):file_hash(p) for p in sorted(output.rglob('*.parquet'))})
    (output/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2)+'\n')
    return manifest
