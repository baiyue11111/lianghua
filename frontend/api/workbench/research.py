"""Research file adapter. Archived results are visible, never implicitly qualified."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
from typing import Protocol

class ResearchAdapter(Protocol):
    def list(self) -> list[dict]: ...
    def detail(self,run_id:str,offset:int,limit:int) -> dict: ...

class FileResearch:
    def __init__(self,root:Path,config:dict): self.root=root.resolve(); self.config=config
    def paths(self):
        files=[]
        for directory in self.config['roots']:
            path=(self.root/directory).resolve()
            if not path.is_relative_to(self.root): raise ValueError('research root escapes project')
            files.extend(p for p in path.glob('*/manifest.json') if (p.parent/'metrics.json').exists())
        return sorted(files)
    def identity(self,path): return hashlib.sha256(str(path.relative_to(self.root)).encode()).hexdigest()
    def read(self,path):
        manifest=json.loads(path.read_bytes()); metrics=json.loads((path.parent/'metrics.json').read_bytes())
        return {'id':self.identity(path),'run_id':manifest['run_id'],'strategy_id':manifest['strategy_id'],
                'status':manifest['status'],'qualification':'unverified',
                'limitations':manifest.get('limitations',[]),'metrics':metrics,
                'manifest_file':str(path.relative_to(self.root)),
                'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'metrics_sha256':hashlib.sha256((path.parent/'metrics.json').read_bytes()).hexdigest(),
                'source_hashes':manifest.get('source_hashes',{}),'config_hash':manifest.get('config_hash'),
                'note':'存档运行，历史数据与会计验收未由本接口证明；指标仅供审核。',
                'parameters':manifest.get('parameters',{}),'effective_config':manifest.get('effective_config',{}),
                'code_hash':manifest.get('code_hash'),'data_hash':manifest.get('data_hash'),'execution_hash':manifest.get('execution_hash')}
    def list(self): return [self.read(p) for p in self.paths()]
    def detail(self,run_id,offset=0,limit=50):
        path=next((p for p in self.paths() if self.identity(p)==run_id),None)
        if path is None: raise KeyError(run_id)
        value=self.read(path); evidence=[]; trades=[]; equity=[]
        for name in ('fills','equity'):
            file=path.parent/'audit'/f'{name}.csv'
            if not file.exists(): continue
            payload=file.read_bytes()
            evidence.append({'file':str(file.relative_to(self.root)),'sha256':hashlib.sha256(payload).hexdigest()})
            with file.open() as stream: rows=list(csv.DictReader(stream))
            if name=='fills': trades=rows[offset:offset+limit]; value['trades_total']=len(rows)
            else:
                stride=max(1,(len(rows)+799)//800)
                sample=rows[::stride]
                if rows and sample[-1]!=rows[-1]:sample.append(rows[-1])
                equity=[{'at':r['trade_date'],'equity':float(r['equity'])} for r in sample]
                value['equity_sampling']={'total':len(rows),'stride':stride}
        return {**value,'manifest':json.loads(path.read_bytes()),'trades':trades,'equity':equity,
                'evidence':evidence,'offset':offset,'limit':limit}
