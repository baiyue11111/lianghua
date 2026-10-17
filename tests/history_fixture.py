"""Engineering inputs only. This never registers a production dataset."""
from datetime import date, timedelta
from pathlib import Path
import hashlib
import json
import pyarrow as pa
import pyarrow.parquet as pq
from strategy import load_strategy, canonical_hash


def make_snapshot(root:Path, history_start=date(2019,7,1), *, symbols=None):
    from protocal import normalize_security_id
    folder=root/'data'/'source'/'fixture'; folder.mkdir(parents=True)
    (folder/'daily').mkdir(); (folder/'factors').mkdir(); (folder/'basic').mkdir()
    days=[]; day=history_start
    while day<=date(2020,2,5):
        if day.weekday()<5: days.append(day)
        day+=timedelta(days=1)
    sessions=days; days=days[:-1]
    symbols=tuple(f'600{i:03d}' for i in range(20)) if symbols is None else tuple(symbols)
    rows=[]
    for symbol in symbols:
        for day in days:
            price=9.
            if day>=date(2020,1,20): price=10.
            if day>=date(2020,1,31): price=11.
            if day>=date(2020,2,4): price=12.
            amount=60_000_000.+int(symbol[-3:])*1_000_000.
            rows.append({'code':symbol,'date':day.isoformat(),'name':'engineering_fixture','open':price,'high':price,'low':price,'close':price,'volume':amount/price,'amount':amount,'tradestatus':1.})
    pq.write_table(pa.Table.from_pylist(rows),folder/'daily'/'bars.parquet')
    files=[]
    def write(name,value,role='market'):
        path=folder/name; path.parent.mkdir(exist_ok=True,parents=True)
        path.write_text(json.dumps(value,ensure_ascii=False))
        files.append({'file':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'role':role})
    files.append({'file':'daily/bars.parquet','sha256':hashlib.sha256((folder/'daily/bars.parquet').read_bytes()).hexdigest(),'role':'market'})
    write('calendar_source.json',{'type':'engineering_fixture',
        'request':{'start':history_start.isoformat(),'end':sessions[-1].isoformat()},
        'sessions':[d.isoformat() for d in sessions]})
    write('calendar.json',{'sessions':[d.isoformat() for d in sessions],'source_file':'calendar_source.json'})
    write('market_members.json', {'records': [
        {'code': symbol, 'index': None, 'effective': history_start.isoformat(),
         'known_at': f'{history_start.isoformat()}T08:00:00+08:00', 'included': True}
        for symbol in symbols
    ]})
    write('code_changes.json',{'schema':1,'scope':'engineering_fixture','events':[]})
    factor_start='1990-01-01'; factor_end='2020-02-05'; corp=[]
    def cache(name,request,fields,rows):
        raw={'code':request['code'],'error_code':'0','fields':fields,'rows':rows}
        write(name,{'schema':2,'request':request,'request_sha256':canonical_hash(request),
            'raw_response':raw,'raw_response_sha256':canonical_hash(raw),
            'request_fields_sha256':hashlib.sha256(','.join(fields).encode()).hexdigest(),'status':'ok' if rows else 'empty'})
    for symbol in symbols:
        identity=normalize_security_id(symbol)
        request={'kind':'adjust','code':identity,'start':factor_start,'end':factor_end,'year':None}
        cache(f'factors/adjust_{symbol}_{factor_start}_{factor_end}.json',request,
            ['code','dividOperateDate','foreAdjustFactor','backAdjustFactor','adjustFactor'],[[identity,'1990-01-01','1','1','1']])
        for year in map(str,range(history_start.year,2021)):
            name=f'corp/dividend_{symbol}_{year}.json'
            cache(name,{'kind':'dividend','code':identity,'start':None,'end':None,'year':year},
                ['code','dividPlanAnnounceDate','dividOperateDate','dividRegistDate','dividPayDate','dividStockMarketDate','dividCashPsBeforeTax','dividStocksPs','dividReserveToStockPs'],[])
            corp.append({'file':name})
    write('policy_source.json',{'type':'engineering_fee_and_tax_policy'},'account')
    policy={'schema':1,'investor':'domestic_individual_unrestricted_a_share',
        'sources':[{'file':'policy_source.json','sha256':files[-1]['sha256']}],
        'fees':{'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
            'stamp_rates':[{'effective':'2015-09-09','rate':'.001'},{'effective':'2023-08-28','rate':'.0005'}],
            'transfer_rates':[{'effective':'2015-09-09','rate':'.00002'}]},
        'tax':{'effective':'2015-09-09','policy':'财税2015-101','payment_timing':'later_of_disposal_and_income',
               'liability':'current_holding_period','rounding':'none'}}
    write('account_policy.json',policy,'account')
    (folder/'manifest.json').write_text(json.dumps({'schema':2,'files':files,'fields':{},'calendar':'calendar.json',
        'scope':{'symbols':symbols,'universe':'CN_A_SHARE'},
        'inputs':{'calendar':{'sessions':'calendar.json','source':'calendar_source.json'},
                  'bars':'daily',
                  'code_changes':'code_changes.json',
                  'factor_cache':{'template':'factors/adjust_{symbol}_{start}_{end}.json','start':factor_start,'end':factor_end},
                  'corporate_action_cache':{'template':'corp/dividend_{symbol}_{year}.json'},
                  'stock_basic_cache':{'template':'basic/stock_basic_{provider_code}.json'},
                  'account_policy':'account_policy.json','action_facts':[]}}))
    registry = root/'data'/'source'/'registry'/'sources.json'
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'sources': [], 'datasets': [], 'benchmarks': [], 'markets': [{
        'scope': 'CN_A', 'dataset_id': 'fixture', 'currency': 'CNY',
        'calendar': 'calendar.json', 'timezone': 'Asia/Shanghai',
        'session_open': '09:30', 'session_close': '15:00',
        'execution': 'cn_a_share', 'code_normalizer': 'normalize_security_id',
        'fields': {'close': {'source': 'daily', 'column': 'close', 'type': 'number'}},
        'members': {'source': 'fixture', 'file': 'market_members.json',
                    'code': 'code', 'index': 'index', 'effective': 'effective',
                    'known': 'known_at', 'included': 'included'},
    }]}))
    return {'dataset': 'fixture'}


def update_manifest(root: Path, **changes):
    path = Path(root) / 'data' / 'source' / 'fixture' / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest.update(changes)
    path.write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest
