"""Normalize a captured dividend snapshot with explicitly reviewed source facts.

No strategy, trading, implicit corrections, or data-lake writes. The result is
an unreleased candidate, not proof of full market coverage or tax-complete NAV.
Run with: python -m tools.corporate_action_candidate ...
"""
import argparse
import csv
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path

from core.calculation import normalize_dividend_records, InvalidCorporateAction
from collect.methods import Request, _validate_cache


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def read_reviews(folder):
    grouped={}; keys=set()
    for path in sorted(folder.glob('*_review.json')):
        review=json.loads(path.read_bytes())
        if review.get('schema')!=1: raise ValueError('unsupported review schema')
        key=(review['code'],review['operation_date'])
        if key in keys: raise ValueError('duplicate reviewed event')
        keys.add(key)
        for field in ('governing_source','resumption_source'):
            if field not in review: continue
            source=review[field]; name=source['file']
            if Path(name).name!=name or digest(folder/name)!=source['sha256']:
                raise ValueError('review source hash mismatch or unsafe path')
        if 'governing_source' not in review: raise ValueError('governing source required')
        grouped.setdefault(Path(review['raw_provider_file']).name,[]).append((review,digest(path)))
    return grouped


def read_resolutions(path):
    """Load explicit announcement-backed row bindings, not inferred identities."""
    if path is None:
        return {}
    path=Path(path); payload=json.loads(path.read_bytes())
    if payload.get('schema')!=1: raise ValueError('unsupported resolution schema')
    grouped={}; keys=set(); review_hash=digest(path)
    for review in payload['reviews']:
        name=review['document']
        if Path(name).name!=name or digest(path.parent/name)!=review['document_sha256']:
            raise ValueError('resolution source hash mismatch or unsafe path')
        source=review['provider_file']
        if Path(source).name!=source: raise ValueError('unsafe provider file')
        for binding in review['rows']:
            index=binding['row']; identity=binding['distribution_id']
            if isinstance(index,bool) or not isinstance(index,int) or index<0:
                raise ValueError('source row must be a nonnegative integer')
            if not isinstance(identity,str) or not identity.strip():
                raise ValueError('explicit distribution identity required')
            key=(source,index)
            if key in keys: raise ValueError('duplicate resolution binding')
            keys.add(key)
        grouped.setdefault(source,[]).append((review,review_hash))
    return grouped


def _load_records(path,reviews,resolutions):
    payload=json.loads(path.read_bytes()); request=payload['request']
    if request['kind']!='dividend': raise ValueError('dividend request required')
    _validate_cache(Request('dividend',request['code'],None,None,request['year']),payload)
    raw=payload['raw_response']; records=[dict(zip(raw['fields'],row)) for row in raw['rows']]
    source_hash=digest(path); changes=[]; matched=[]
    for review,review_hash in reviews.get(path.name,[]):
        if review['raw_provider_sha256']!=source_hash: raise ValueError('review raw source hash mismatch')
        matching=[r for r in records if r['code'].split('.')[-1]==review['code']
                  and r['dividOperateDate']==review['operation_date']]
        if len(matching)!=1: raise ValueError('review does not bind one source event')
        record=matching[0]
        for change in review['corrected_fields']:
            field=change['field']
            if record[field]!=change['before']: raise ValueError('review field before-value mismatch')
            record[field]=change['after']
            changes.append(dict(code=review['code'],operation_date=review['operation_date'],
                                **change,review_sha256=review_hash,source_sha256=source_hash))
        for field in ('stock_credit_date','stock_tradable_from'):
            if field in review:
                date.fromisoformat(review[field]); record[field]=review[field]
        record['review_sha256']=review_hash; matched.append(review_hash)
    for review,review_hash in resolutions.get(path.name,[]):
        if review['provider_sha256']!=source_hash: raise ValueError('resolution raw source hash mismatch')
        for binding in review['rows']:
            index=binding['row']
            if index>=len(records): raise ValueError('resolution source row missing')
            record=records[index]
            field='dividCashPsBeforeTax'; before=binding['before_cash_per_share']
            if record[field]!=before: raise ValueError('resolution before-value mismatch')
            after=binding['implemented_cash_per_share']
            if before!=after:
                changes.append(dict(code=record['code'],operation_date=record['dividOperateDate'],
                                    field=field,before=before,after=after,source_row=index,
                                    review_sha256=review_hash,source_sha256=source_hash))
            record[field]=after; record['distribution_id']=binding['distribution_id']
            record['resolution_sha256']=review_hash
        matched.append(review_hash)
    return records,dict(source_file=path.name,source_sha256=source_hash,
                       changes=changes,matched_reviews=matched)


def load_records(path,review_dir):
    return _load_records(Path(path),read_reviews(Path(review_dir)),{})


def build_candidate(cache,review_dir,output,*,resolutions=None):
    cache,review_dir,output=map(Path,(cache,review_dir,output))
    if output.exists(): raise ValueError('new output directory required')
    paths=sorted(cache.glob('dividend_*.json'))
    if not paths: raise ValueError('empty source snapshot')
    reviews=read_reviews(review_dir); plans=read_resolutions(resolutions)
    events=[]; rejected=[]; sources=[]; changes=[]; used=set(); row_count=0
    for path in paths:
        records,audit=_load_records(path,reviews,plans)
        sources.append(dict(file=path.name,sha256=audit['source_sha256']))
        changes.extend(audit['changes']); used.update(audit['matched_reviews']); row_count+=len(records)
        for index,record in enumerate(records):
            try:
                event=normalize_dividend_records([record])[0]
            except InvalidCorporateAction as exc:
                rejected.append(dict(source_file=path.name,source_sha256=audit['source_sha256'],
                                     row=index,record=record,error=str(exc)))
                continue
            normalized={key:value.isoformat() if isinstance(value,date) else value
                        for key,value in asdict(event).items()}
            normalized.update(source_file=path.name,source_row=index,source_sha256=audit['source_sha256'],
                              review_sha256=record.get('review_sha256'),
                              distribution_id=record.get('distribution_id'),
                              resolution_sha256=record.get('resolution_sha256'),
                              stock_credit_date=record.get('stock_credit_date'),
                              stock_tradable_from=record.get('stock_tradable_from'))
            normalized['issues']=json.dumps(normalized['issues'],ensure_ascii=False)
            events.append(normalized)
    expected={value[1] for values in reviews.values() for value in values}
    expected.update(value[1] for values in plans.values() for value in values)
    missing_plan_sources=set(plans)-{p.name for p in paths}
    if missing_plan_sources: raise ValueError('resolution sources missing from snapshot')
    if used!=expected: raise ValueError('unused reviews: captured source snapshot is incomplete')
    events.sort(key=lambda r:(r['code'],r['operation_date'],r['source_file'],r['source_row']))
    output.mkdir(parents=True)
    for name,value in [('events.json',events),('rejected.json',rejected),('changes.json',changes)]:
        (output/name).write_bytes(encoded(value)+b'\n')
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pylist(events),output/'events.parquet',compression='zstd')
    with (output/'events.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(events[0]) if events else ['code','operation_date'])
        writer.writeheader(); writer.writerows(events)
    manifest=dict(schema=1,release_status='NOT_RELEASED',scope='all captured dividend responses; no strategy or security filter',
                  counts=dict(source_files=len(paths),source_rows=row_count,normalized_events=len(events),
                              rejected_rows=len(rejected),review_documents=len(used),
                              reviewed_events=sum(e['review_sha256'] is not None or e['resolution_sha256'] is not None for e in events),
                              field_changes=len(changes),
                              nonexecutable_events=sum(not e['executable'] for e in events),
                              plan_identified_observations=sum(e['distribution_id'] is not None for e in events),
                              share_events_without_credit_date=sum(e['share_ratio']>1 and e['stock_credit_date'] is None for e in events)),
                  sources=sources,reviews=sorted(used),
                  unresolved_gates=['full_market_inventory','tax_and_fractional_allocation','identity_reconciliation','backtest_integration'],
                  code_sha256={p.name:digest(p) for p in [Path(__file__),Path(normalize_dividend_records.__code__.co_filename),Path(_validate_cache.__code__.co_filename)]},
                  output_sha256={p.name:digest(p) for p in sorted(output.iterdir())})
    manifest['run_id']=hashlib.sha256(encoded(manifest)).hexdigest()
    (output/'manifest.json').write_bytes(encoded(manifest)+b'\n')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--reviews',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resolutions',type=Path,help='explicit announcement-backed distribution identities and implemented terms')
    args=parser.parse_args()
    result=build_candidate(args.cache,args.reviews,args.output,resolutions=args.resolutions)
    print(json.dumps(dict(status=result['release_status'],run_id=result['run_id'],**result['counts']),ensure_ascii=False))
