"""Project existing market sources into one reusable, parameter-free dataset.

Run with an explicit JSON source configuration. Original provider responses
remain byte-identical; derivation.json records the source file hashes.
"""
import argparse
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from corporate_actions import normalize_dividend_records
from protocal import DataError, DataRef, build_data


def _ref(value, label):
    if not isinstance(value, dict) or set(value) != {"kind", "identity", "name", "file", "sha256", "size"}:
        raise ValueError(f"{label} source DataRef is required")
    try:
        return DataRef(**value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} source DataRef is invalid") from error


def _read(data, ref, label):
    try:
        return data.read_ref(ref)
    except DataError as error:
        if error.code == 'source_hash_mismatch':
            raise ValueError(f"{label} source hash mismatch") from error
        if error.code == 'invalid_ref':
            raise ValueError(f"{label} source identity mismatch") from error
        raise ValueError(f"{label} source is missing") from error
    except Exception as error:
        raise ValueError(f"{label} source is missing") from error


def _source(data, refs, file, label):
    file = Path(file).as_posix()
    matches = [ref for ref in refs if ref.file == file]
    if len(matches) != 1:
        raise ValueError(f"{label} source DataRef is missing")
    return matches[0], _read(data, matches[0], label)


def _source_in_identity(data, refs, parent, name, label):
    matches = [ref for ref in refs
               if ref.kind == parent.kind and ref.identity == parent.identity and ref.name == name]
    if len(matches) != 1:
        raise ValueError(f"{label} source DataRef is missing")
    return matches[0], _read(data, matches[0], label)


def prepare(root, config, *, data=None):
    root=Path(root).resolve()
    data = build_data(root) if data is None else data
    refs = data.list('sources')
    dataset_id=config['dataset_id']
    policy_ref = _ref(config.get('account_policy_ref'), 'account policy')
    if policy_ref.kind == 'sources' and policy_ref.identity == dataset_id:
        raise ValueError('account policy source identity must be independent from dataset output')
    policy_bytes = _read(data, policy_ref, 'account policy')
    symbols=tuple(config['symbols'])
    if not symbols or tuple(sorted(set(symbols)))!=symbols:raise ValueError('ordered unique symbols required')
    start,end=date.fromisoformat(config['history_start']),date.fromisoformat(config['end'])
    calendar_ref, calendar_bytes = _source(data, refs, config['calendar_source'], 'calendar')
    raw_calendar=json.loads(calendar_bytes)
    response=raw_calendar['response']
    if raw_calendar['provider']!='BaoStock' or response['error_code']!='0':raise ValueError('successful calendar source required')
    observed=[dict(zip(response['fields'],row,strict=True)) for row in response['rows']]
    dates=[date.fromisoformat(row['calendar_date']) for row in observed]
    requested=raw_calendar['request'];first=date.fromisoformat(requested['start']);last=date.fromisoformat(requested['end'])
    if dates!=[first+timedelta(days=i) for i in range((last-first).days+1)]:raise ValueError('incomplete calendar response')
    if not first<=start<=end<last:raise ValueError('calendar must cover requested range and next session')
    if any(row['is_trading_day'] not in ('0','1') for row in observed):raise ValueError('invalid trading-day flag')
    sessions=[row['calendar_date'] for row in observed if row['is_trading_day']=='1' and row['calendar_date']>=start.isoformat()]
    evaluated=[day for day in sessions if day<=end.isoformat()]
    following=next(day for day in sessions if day>end.isoformat())
    prefix = Path(config['daily_dir']).as_posix().rstrip('/') + '/'
    daily_refs = sorted(
        (ref for ref in refs if ref.file.startswith(prefix) and ref.name.endswith('.parquet')),
        key=lambda ref: (ref.identity, ref.name),
    )
    if not daily_refs:raise ValueError('missing daily market sources')
    tables = [pq.read_table(BytesIO(_read(data, ref, 'daily market'))) for ref in daily_refs]
    table = pa.concat_tables(tables)
    with duckdb.connect() as connection:
        connection.register('daily_source', table)
        table=connection.execute('SELECT * FROM daily_source WHERE code IN (SELECT unnest(?)) AND date BETWEEN ? AND ? ORDER BY code,date',
            [list(symbols),start.isoformat(),end.isoformat()]).to_arrow_table()
    rows=table.to_pylist()
    if sorted({row['date'] for row in rows})!=evaluated:raise ValueError('price sessions differ from sourced calendar')
    if tuple(sorted({row['code'] for row in rows}))!=symbols:raise ValueError('missing requested securities')
    if len({(row['code'],row['date']) for row in rows})!=len(rows):raise ValueError('duplicate source price rows')
    code_ref, code_bytes = _source(data, refs, config['code_changes'], 'code changes')
    policy=json.loads(policy_bytes)
    policy_inputs=[]
    for source in policy['sources']:
        policy_source_ref, policy_source_bytes = _source_in_identity(
            data, refs, policy_ref, source['file'], 'account policy'
        )
        if sha256(policy_source_bytes).hexdigest()!=source['sha256']:
            raise ValueError('account policy source hash mismatch')
        policy_inputs.append((source, policy_source_ref, policy_source_bytes))

    factor_inputs=[];corp_inputs=[];coverage=[];facts=[]
    for symbol in symbols:
        factor=f"adjust_{symbol}_{config['factor_start']}_{config['factor_end']}.json"
        factor_ref, factor_bytes = _source(data, refs, f"{config['cache_dir'].rstrip('/')}/{factor}", 'factor')
        factor_inputs.append((factor, factor_ref, factor_bytes))
        for year in range(start.year,end.year+1):
            name=f'dividend_{symbol}_{year}.json'
            source_ref, source_bytes=_source(data, refs, f"{config['cache_dir'].rstrip('/')}/{name}", 'corporate action')
            payload=json.loads(source_bytes);raw=payload['raw_response']
            output_name='corp/'+name
            coverage.append({'file':output_name})
            corp_inputs.append((output_name, source_ref, source_bytes))
            for index,row in enumerate(raw['rows']):
                values=dict(zip(raw['fields'],row,strict=True));event=normalize_dividend_records([values])[0]
                if event.registration_date is None or not start<=event.registration_date<=end:continue
                if event.stock_dividend_per_share or event.reserve_to_stock_per_share:
                    raise ValueError('share deliveries require explicit sourced action facts')
                announcement=values['dividPlanAnnounceDate']
                if not announcement:raise ValueError('distribution identity requires plan announcement date')
                identity=f"{symbol}:{announcement}:{event.registration_date}"
                facts.append({'source_file':output_name,'source_row':index,'distribution_id':identity,
                    'credit_date':None,'credit_source_file':None,'stock_nominal_per_share':'1',
                    'reserve_taxable_per_share':'0','tax_source_file':policy['sources'][0]['file']})

    evidence=[];derivation={}
    def write_bytes(name,value,role='market'):
        ref=data.write('sources',dataset_id,name,value)
        evidence.append({'file':name,'sha256':ref.sha256,'role':role})
    def write(name,value,role='market'):
        write_bytes(name,json.dumps(value,ensure_ascii=False,indent=2).encode()+b'\n',role)
    def copy(source_ref,source_bytes,name,role='market'):
        write_bytes(name,source_bytes,role)
        derivation[source_ref.file]=source_ref.sha256
    for source_ref in daily_refs:derivation[source_ref.file]=source_ref.sha256
    buffer=BytesIO();pq.write_table(table,buffer,compression='zstd');write_bytes('daily/bars.parquet',buffer.getvalue())
    copy(calendar_ref,calendar_bytes,'calendar_source.json')
    write('calendar.json',{'sessions':evaluated+[following],'source_file':'calendar_source.json'})
    copy(code_ref,code_bytes,'code_changes.json')
    write_bytes('account_policy.json',policy_bytes,'account');derivation[policy_ref.file]=policy_ref.sha256
    for source, policy_source_ref, policy_source_bytes in policy_inputs:
        write_bytes(source['file'],policy_source_bytes,'account')
        derivation[policy_source_ref.file]=policy_source_ref.sha256
    for factor, factor_ref, factor_bytes in factor_inputs:
        copy(factor_ref,factor_bytes,'factors/'+factor)
    for output_name, source_ref, source_bytes in corp_inputs:
        copy(source_ref,source_bytes,output_name)
    write('derivation.json',{'sources':derivation,'query':{'symbols':symbols,'start':start.isoformat(),'end':end.isoformat()},
        'limitations':['固定证券样本，不证明全市场完整性；分红与因子为供应商历史响应，不是独立公司公告全量核对。']})
    manifest={'schema':2,'files':evidence,'fields':config['fields'],'calendar':'calendar.json','scope':{'symbols':symbols,'universe':config['universe']},
        'price':{'daily_dir':'daily','cache_dir':'factors','stock_basic_dir':'basic','code_changes':'code_changes.json',
            'factor_start':config['factor_start'],'factor_end':config['factor_end']},
        'corporate_action_sources':coverage,'action_facts':facts,'account_policy':'account_policy.json'}
    data.publish(dataset_id,manifest)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--root',default='.')
    args=parser.parse_args();root=Path(args.root).resolve()
    result=prepare(root,json.loads((root/args.config).read_bytes()))
    print(f"Prepared {len(result['scope']['symbols'])} securities; runtime input validation still required.")
