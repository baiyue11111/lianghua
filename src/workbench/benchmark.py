"""Configured price benchmarks, read through the same immutable source store."""
from math import isfinite

class Benchmarks:
    def __init__(self,store,specs):
        self.store=store;self.specs={s['id']:s for s in specs}
        if len(self.specs)!=len(specs):raise ValueError('duplicate benchmark identity')
    def list(self):return list(self.specs.values())
    def series(self,identity,start,end,*,dates=None):
        if identity not in self.specs:raise KeyError(identity)
        if start>end:raise ValueError('起始日期不能晚于截止日期')
        spec=self.specs[identity];snapshot=self.store.latest(spec['source_id'])
        if snapshot is None:raise ValueError('基准未同步，请在数据同步页面采集')
        points=[];seen=set()
        for row in snapshot['records']:
            if row['code']!=spec['code'] or not start<=row['date']<=end:continue
            value=float(row['close'])
            if not isfinite(value) or value<=0:raise ValueError('基准收盘价必须为正数')
            if row['date'] in seen:raise ValueError('基准交易日期重复')
            seen.add(row['date']);points.append({'at':row['date'],'close':value})
        points=sorted(points,key=lambda p:p['at'])
        if not points:raise ValueError('基准快照在请求区间没有数据')
        if dates is not None and tuple(item['at'] for item in points)!=tuple(dates):
            raise ValueError('基准日期不全或顺序不一致')
        return {**spec,'snapshot_id':snapshot['snapshot_id'],'collected_at':snapshot['collected_at'],
                'start':start,'end':end,'points':points}
