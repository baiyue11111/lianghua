"""Quote collectors. Parameters and results pass through collection interaction."""
from __future__ import annotations
import contextlib
import io
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode
from .data import http, CollectionError


def fetch_bars(request):
    from urllib.request import Request, urlopen
    url = request['url'] + '?' + urlencode(request['params'])
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as response:
        return response.read().decode('utf-8')


def raw_response(request, timeout):
    import baostock as bs
    from .factors import _timeout, _response_rows, login_with_timeout
    login_with_timeout(bs, timeout)
    try:
        with _timeout(timeout):
            response = bs.query_history_k_data_plus(
                request['code'], request['fields'], start_date=request['start_date'],
                end_date=request['end_date'], frequency='d', adjustflag='3')
        fields, rows = _response_rows(response, timeout)
        return {'error_code': response.error_code, 'error_msg': response.error_msg,
                'fields': fields, 'rows': rows}
    finally:
        bs.logout()

def fetch_sina_factors(symbol: str) -> list[tuple[str, float]]:
    from urllib.request import Request, urlopen
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("invalid security code")
    code = ("sh" if symbol[0] in "69" else "sz") + symbol
    url = f"https://finance.sina.com.cn/realstock/company/{code}/hfq.js"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=25) as response:
        text = response.read().decode("utf-8")
    match = re.search(r"=\s*\{", text)
    if not match:
        raise ValueError("missing adjustment-factor payload")
    payload, _ = json.JSONDecoder().raw_decode(text[match.end() - 1:])
    rows = [(row["d"], float(row["f"])) for row in payload["data"]]
    return sorted(rows)


class EastmoneyQuotes:
    def __init__(self,config,transport:Callable=http): self.config=config; self.transport=transport
    def collect(self,request):
        raw=[]
        try: return self._collect(request, raw)
        except Exception as error: raise CollectionError(str(error), raw) from error
    def _collect(self,request,raw):
        names = request.get('fields')
        allowed = {'name', 'price', 'change_pct', 'volume', 'amount'}
        if names is not None and (not names or not set(names) <= allowed or len(names) != len(set(names))):
            raise ValueError('invalid quote fields')
        if request.get('frequency', 'd') != 'd' or request.get('adjustment', '3') != '3':
            raise ValueError('latest quotes do not support historical frequency or adjustment')
        codes=request['codes']
        if len(codes)!=len(set(codes)): raise ValueError('duplicate quote codes')
        if not codes or any(not re.fullmatch(r'[01]\.\d{6}',c) for c in codes): raise ValueError('quotes codes require market.number e.g. 1.600000')
        fields='f2,f3,f4,f5,f6,f12,f13,f14,f124'
        url='https://push2.eastmoney.com/api/qt/ulist.np/get?'+urlencode({'secids':','.join(codes),'fields':fields,'fltt':'2'})
        response=self.transport(url)
        raw.append({'url':url,'request':request,'response':response})
        rows=response.get('data',{}).get('diff')
        if not isinstance(rows,list) or len(rows)!=len(codes): raise ValueError('quote response missing requested securities')
        result=[]
        for row in rows:
            code=f"{row['f13']}.{row['f12']}"
            if code not in codes: raise ValueError('unexpected quote security')
            if type(row.get('f124')) is not int or row['f124'] <= 0: raise ValueError('missing quote timestamp')
            for field in ('f2', 'f3', 'f5', 'f6'):
                value = row.get(field)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError('invalid quote value')
            if any(row[field] < 0 for field in ('f2', 'f5', 'f6')):
                raise ValueError('negative quote value')
            result.append({'code':code,'name':row['f14'],'price':row['f2'],'change_pct':row['f3'],
                           'volume':row['f5'],'amount':row['f6'],
                           'data_at':datetime.fromtimestamp(row['f124'],timezone.utc).isoformat(),'raw_page':0})
        if len({r['code'] for r in result})!=len(codes): raise ValueError('duplicate quotes')
        if names is not None:
            result = [{key: value for key, value in row.items() if key in {'code', 'data_at', 'raw_page'} | set(names)} for row in result]
        return {'records':result,'raw':raw,
                'data_as_of':max(r['data_at'] for r in result),'scope':{'codes':codes,'frequency':'provider quote snapshot'}}


class BaostockDaily:
    def __init__(self,config): self.config=config
    def collect(self,request):
        return worker(request)


def worker(request):
    completed = subprocess.run([sys.executable, '-m', 'collect.quotes'],
                               input=json.dumps(request), text=True, capture_output=True, timeout=90)
    result = json.loads(completed.stdout)
    if completed.returncode != 0:
        raise CollectionError(result['error'], result['raw'])
    return result


def daily_frame(code, fields, start, end, frequency='d', adjustment='3'):
    import pandas as pd
    from .factors import _bs_code
    names = fields.split(',')
    requested = names if 'code' in names else [*names, 'code']
    result = worker({'codes': [_bs_code(code)], 'fields': requested, 'start': start, 'end': end,
                     'frequency': frequency, 'adjustment': adjustment})
    return pd.DataFrame(result['records'], columns=names)


def factor_frame(code, start, end):
    import pandas as pd
    from .factors import _bs_code
    result = worker({'operation': 'adjust', 'code': _bs_code(code), 'start': start, 'end': end})
    raw = result['raw'][0]['response']
    return pd.DataFrame(raw['rows'], columns=raw['fields'])


def collect_adjustment(request):
    from .factors import Request, fetch_one, login_with_timeout
    import baostock as bs
    login_with_timeout(bs, 30)
    try:
        payload = fetch_one(Request('adjust', request['code'], request['start'], request['end'], None), bs, timeout=30)
    finally:
        bs.logout()
    return {'raw': [{'request': payload['request'], 'response': payload['raw_response']}], 'records': []}

def collect_daily(request):
    import baostock as bs
    codes=request['codes']
    if not codes or any(not re.fullmatch(r'(sh|sz)\.\d{6}',c) for c in codes): raise ValueError('daily codes require sh.number or sz.number')
    login=bs.login()
    if login.error_code!='0': raise RuntimeError(f'login {login.error_code}: {login.error_msg}')
    records=[]; raw=[]
    try:
        for code in codes:
            frequency=request.get('frequency','d'); adjustment=request.get('adjustment','3')
            names=request.get('fields')
            if names is None:
                names=['date','code','open','high','low','close','volume','amount','adjustflag']
                if frequency in {'5','15','30','60'}:
                    names.insert(1, 'time')
                else:
                    names.append('pctChg')
            allowed={'date','time','code','open','high','low','close','volume','amount','adjustflag','turn','tradestatus','pctChg','peTTM','pbMRQ','psTTM','pcfNcfTTM','isST'}
            if (not {'date','code'} <= set(names)
                    or not set(names) <= allowed or len(names)!=len(set(names))):
                raise ValueError('invalid daily fields')
            if frequency in {'5','15','30','60'} and 'time' not in names:
                raise ValueError('minute bars require time')
            fields=','.join(names)
            if frequency not in {'d','w','m','5','15','30','60'} or adjustment not in {'1','2','3'}:
                raise ValueError('invalid frequency or adjustment')
            response=bs.query_history_k_data_plus(code,fields,start_date=request['start'],end_date=request['end'],frequency=frequency,adjustflag=adjustment)
            if response.error_code!='0': raise RuntimeError(response.error_msg)
            rows=[]
            while response.next(): rows.append(response.get_row_data())
            if response.error_code!='0': raise RuntimeError(response.error_msg)
            if response.fields!=fields.split(','): raise ValueError('daily field contract changed')
            raw.append({'provider':'baostock','request':{**request,'code':code,'fields':fields,'adjustflag':adjustment},
                        'response':{'fields':response.fields,'rows':rows,'error_code':response.error_code}})
            for row in rows:
                if len(row)!=len(response.fields): raise ValueError('daily row length mismatch')
                value=dict(zip(response.fields,row))
                if value['code']!=code or not request['start']<=value['date']<=request['end']: raise ValueError('daily request/response mismatch')
                for name in {'open','high','low','close'} & value.keys(): float(value[name])
                value['raw_page']=len(raw) - 1
                records.append(value)
    except Exception as error:
        raise CollectionError(str(error), raw) from error
    finally: bs.logout()
    if len({(r['code'],r['date'],r.get('time')) for r in records})!=len(records): raise ValueError('duplicate bar keys')
    return {'records':records,'raw':raw,'data_as_of':max((r['date'] for r in records),default=None),
            'scope':{'codes':codes,'start':request['start'],'end':request['end'],'frequency':request.get('frequency','d'),
                     'adjustment':request.get('adjustment','3')}}
if __name__=='__main__':
    request=json.loads(sys.stdin.read())
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = {'daily': collect_daily, 'adjust': collect_adjustment}[request.get('operation', 'daily')](request)
    except Exception as error:
        print(json.dumps({'error': str(error), 'raw': getattr(error, 'evidence', [])}, ensure_ascii=False, allow_nan=False))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
