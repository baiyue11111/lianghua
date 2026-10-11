"""One isolated provider session; caller imposes a hard process timeout."""
import contextlib
import io
import json
import sys
import re
import baostock as bs

def collect(request):
    codes=request['codes']
    if not codes or any(not re.fullmatch(r'(sh|sz)\.\d{6}',c) for c in codes): raise ValueError('daily codes require sh.number or sz.number')
    login=bs.login()
    if login.error_code!='0': raise RuntimeError(f'login {login.error_code}: {login.error_msg}')
    records=[]; raw=[]
    try:
        for code in codes:
            fields='date,code,open,high,low,close,volume,amount,adjustflag,pctChg'
            response=bs.query_history_k_data_plus(code,fields,start_date=request['start'],end_date=request['end'],frequency='d',adjustflag='3')
            if response.error_code!='0': raise RuntimeError(response.error_msg)
            rows=[]
            while response.next(): rows.append(response.get_row_data())
            if response.error_code!='0': raise RuntimeError(response.error_msg)
            if response.fields!=fields.split(','): raise ValueError('daily field contract changed')
            for row in rows:
                if len(row)!=len(response.fields): raise ValueError('daily row length mismatch')
                value=dict(zip(response.fields,row))
                if value['code']!=code or not request['start']<=value['date']<=request['end']: raise ValueError('daily request/response mismatch')
                for name in ('open','high','low','close'): float(value[name])
                value['raw_page']=len(raw)
                records.append(value)
            raw.append({'provider':'baostock','request':{**request,'code':code,'fields':fields,'adjustflag':'3'},
                        'response':{'fields':response.fields,'rows':rows,'error_code':response.error_code}})
    finally: bs.logout()
    if len({(r['code'],r['date']) for r in records})!=len(records): raise ValueError('duplicate daily keys')
    return {'records':records,'raw':raw,'data_as_of':max((r['date'] for r in records),default=None),
            'scope':{'codes':codes,'start':request['start'],'end':request['end'],'frequency':'daily','adjustment':'raw'}}
if __name__=='__main__':
    request=json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(io.StringIO()): result=collect(request)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
