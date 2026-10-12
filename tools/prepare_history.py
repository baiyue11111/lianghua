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
import pyarrow.parquet as pq

from corporate_actions import normalize_dividend_records
from protocal import build_data


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def prepare(root, config, *, data=None):
    root=Path(root).resolve()
    data = build_data(root) if data is None else data
    def path(name):
        result=(root/name).resolve()
        if not result.is_relative_to(root):raise ValueError('source path escapes project')
        return result
    dataset_id=config['dataset_id']
    manifest_path=data.path('sources',dataset_id,'manifest.json')
    folder=manifest_path.parent
    folder.mkdir(parents=True,exist_ok=True)
    # Remove only the previous release marker, never hide a failed rebuild.
    if manifest_path.exists(): data.delete('sources',dataset_id,'manifest.json')
    for name in ('daily','factors','basic','corp'):(folder/name).mkdir(exist_ok=True)
    symbols=tuple(config['symbols'])
    if not symbols or tuple(sorted(set(symbols)))!=symbols:raise ValueError('ordered unique symbols required')
    start,end=date.fromisoformat(config['history_start']),date.fromisoformat(config['end'])
    raw_calendar=json.loads(path(config['calendar_source']).read_bytes())
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
    files=sorted(path(config['daily_dir']).rglob('*.parquet'))
    if not files:raise ValueError('missing daily market sources')
    with duckdb.connect() as connection:
        table=connection.execute('SELECT * FROM read_parquet(?) WHERE code IN (SELECT unnest(?)) AND date BETWEEN ? AND ? ORDER BY code,date',
            [[str(p) for p in files],list(symbols),start.isoformat(),end.isoformat()]).to_arrow_table()
    rows=table.to_pylist()
    if sorted({row['date'] for row in rows})!=evaluated:raise ValueError('price sessions differ from sourced calendar')
    if tuple(sorted({row['code'] for row in rows}))!=symbols:raise ValueError('missing requested securities')
    if len({(row['code'],row['date']) for row in rows})!=len(rows):raise ValueError('duplicate source price rows')
    refs=[];derivation={}
    def write_bytes(name,value,role='market'):
        ref=data.write('sources',dataset_id,name,value)
        refs.append({'file':name,'sha256':ref.sha256,'role':role})
    def record(name,role):
        refs.append({'file':name,'sha256':digest(data.path('sources',dataset_id,name)),'role':role})
    def write(name,value,role='market'):
        write_bytes(name,json.dumps(value,ensure_ascii=False,indent=2).encode()+b'\n',role)
    def copy(source,name,role='market'):
        write_bytes(name,source.read_bytes(),role)
        derivation[str(source.relative_to(root))]=digest(source)
    for source in files:derivation[str(source.relative_to(root))]=digest(source)
    buffer=BytesIO();pq.write_table(table,buffer,compression='zstd');write_bytes('daily/bars.parquet',buffer.getvalue())
    copy(path(config['calendar_source']),'calendar_source.json')
    write('calendar.json',{'sessions':evaluated+[following],'source_file':'calendar_source.json'})
    copy(path(config['code_changes']),'code_changes.json')
    policy=json.loads(data.read('sources',dataset_id,'account_policy.json'));record('account_policy.json','account')
    for source in policy['sources']:
        if digest(folder/source['file'])!=source['sha256']:raise ValueError('account policy source hash mismatch')
        record(source['file'],'account')
    coverage=[];facts=[]
    for symbol in symbols:
        factor=f"adjust_{symbol}_{config['factor_start']}_{config['factor_end']}.json"
        copy(path(config['cache_dir'])/factor,'factors/'+factor)
        for year in range(start.year,end.year+1):
            name=f'dividend_{symbol}_{year}.json';source=path(config['cache_dir'])/name
            copy(source,'corp/'+name);coverage.append({'file':'corp/'+name})
            payload=json.loads(source.read_bytes());raw=payload['raw_response']
            for index,row in enumerate(raw['rows']):
                values=dict(zip(raw['fields'],row,strict=True));event=normalize_dividend_records([values])[0]
                if event.registration_date is None or not start<=event.registration_date<=end:continue
                if event.stock_dividend_per_share or event.reserve_to_stock_per_share:
                    raise ValueError('share deliveries require explicit sourced action facts')
                announcement=values['dividPlanAnnounceDate']
                if not announcement:raise ValueError('distribution identity requires plan announcement date')
                # Repeated provider observations of one declared plan share an ID.
                identity=f"{symbol}:{announcement}:{event.registration_date}"
                facts.append({'source_file':'corp/'+name,'source_row':index,'distribution_id':identity,
                    'credit_date':None,'credit_source_file':None,'stock_nominal_per_share':'1',
                    'reserve_taxable_per_share':'0','tax_source_file':policy['sources'][0]['file']})
    write('derivation.json',{'sources':derivation,'query':{'symbols':symbols,'start':start.isoformat(),'end':end.isoformat()},
        'limitations':['固定证券样本，不证明全市场完整性；分红与因子为供应商历史响应，不是独立公司公告全量核对。']})
    manifest={'schema':2,'files':refs,'fields':config['fields'],'calendar':'calendar.json','scope':{'symbols':symbols,'universe':config['universe']},
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
