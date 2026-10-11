"""Provider-specific parsing stays here. No automatic retries or alternative sources."""
from __future__ import annotations
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from typing import Callable


def http(url, data=None):
    req=Request(url,data=data,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/',
                                      'Content-Type':'application/x-www-form-urlencoded'})
    with urlopen(req,timeout=25) as response: return json.loads(response.read())

class EastmoneyQuotes:
    def __init__(self,config,transport:Callable=http): self.config=config; self.transport=transport
    def collect(self,request):
        codes=request['codes']
        if not codes or any(not re.fullmatch(r'[01]\.\d{6}',c) for c in codes): raise ValueError('quotes codes require market.number e.g. 1.600000')
        fields='f2,f3,f4,f5,f6,f12,f13,f14,f124'
        url='https://push2.eastmoney.com/api/qt/ulist.np/get?'+urlencode({'secids':','.join(codes),'fields':fields,'fltt':'2'})
        response=self.transport(url)
        rows=response.get('data',{}).get('diff')
        if not isinstance(rows,list) or len(rows)!=len(codes): raise ValueError('quote response missing requested securities')
        result=[]
        for row in rows:
            code=f"{row['f13']}.{row['f12']}"
            if code not in codes: raise ValueError('unexpected quote security')
            if not isinstance(row.get('f124'),int): raise ValueError('missing quote timestamp')
            result.append({'code':code,'name':row['f14'],'price':row['f2'],'change_pct':row['f3'],
                           'volume':row['f5'],'amount':row['f6'],
                           'data_at':datetime.fromtimestamp(row['f124'],timezone.utc).isoformat(),'raw_page':0})
        if len({r['code'] for r in result})!=len(codes): raise ValueError('duplicate quotes')
        return {'records':result,'raw':[{'url':url,'request':request,'response':response}],
                'data_as_of':max(r['data_at'] for r in result),'scope':{'codes':codes,'frequency':'provider quote snapshot'}}


def parse_cninfo_page(response,number,size):
    total=response.get('totalRecordNum'); more=response.get('hasMore'); rows=response.get('announcements')
    if rows is None and total==0: rows=[]
    if type(total)!=int or total<0 or type(more)!=bool or not isinstance(rows,list): raise ValueError('invalid announcement response')
    if len(rows)!=max(0,min(size,total-(number-1)*size)) or more!=(number*size<total): raise ValueError('announcement page disagrees with total')
    records=[]
    for row in rows:
        for k in ('announcementId','orgId','secCode','announcementTitle','adjunctUrl'):
            if not isinstance(row.get(k),str) or not row[k]: raise ValueError(f'missing announcement {k}')
        if not isinstance(row.get('announcementTime'),int): raise ValueError('missing publication date')
        link=urljoin('https://static.cninfo.com.cn/',row['adjunctUrl'])
        if not link.startswith('https://static.cninfo.com.cn/'): raise ValueError('unexpected document host')
        records.append({'id':row['announcementId'],'org_id':row['orgId'],'code':row['secCode'],
                        'name':row.get('secName'),'title':row['announcementTitle'],
                        'published_at':datetime.fromtimestamp(row['announcementTime']/1000,timezone.utc).isoformat(),
                        'report_period':None,'url':link})
    return records,total,more

class CollectionError(RuntimeError):
    def __init__(self,message,evidence):
        super().__init__(message); self.evidence=evidence

class CninfoAnnouncements:
    def __init__(self,config,transport:Callable=http): self.config=config; self.transport=transport
    def collect(self,request):
        raw=[]
        try: return self._collect(request,raw)
        except Exception as exc: raise CollectionError(str(exc),raw) from exc
    def _collect(self,request,raw):
        url='https://www.cninfo.com.cn/new/hisAnnouncement/query'
        size=30; number=1; expected=None; records=[]; seen=set()
        while True:
            query={'pageNum':str(number),'pageSize':str(size),'seDate':request['start']+'~'+request['end'],
                   'tabName':'fulltext','column':'szse','plate':'','stock':'','searchkey':request.get('keyword',''),
                   'category':self.config.get('category',''),'isHLtitle':'true'}
            response=self.transport(url,urlencode(query).encode())
            raw.append({'url':url,'request':query,'response':response})
            rows,total,more=parse_cninfo_page(response,number,size)
            if expected is not None and total!=expected: raise ValueError('announcement total changed during pagination')
            expected=total
            for row in rows:
                key=(row['id'],row['org_id'],row['code'])
                if key in seen: raise ValueError(f'duplicate announcement identity on page {number}: {key}')
                seen.add(key); row['raw_page']=len(raw)-1; records.append(row)
            if not more: break
            number+=1
        return {'records':records,'raw':raw,'data_as_of':max((r['published_at'] for r in records),default=None),
                'scope':{'start':request['start'],'end':request['end'],'keyword':request.get('keyword',''),
                         'category':self.config.get('category',''),'pages':len(raw),'total':expected,
                         'note':'configured query matches; document index only; report periods not inferred'}}

class BaostockDaily:
    def __init__(self,config): self.config=config
    def collect(self,request):
        completed=subprocess.run([sys.executable,'-m','frontend.api.workbench.baostock_worker'],
                                 input=json.dumps(request),text=True,capture_output=True,timeout=90,check=True)
        return json.loads(completed.stdout)
