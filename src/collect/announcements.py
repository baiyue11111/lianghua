"""Announcement and financial-report index collection."""
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urljoin
from .data import http, CollectionError

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
