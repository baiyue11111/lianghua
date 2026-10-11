"""Explicit historical lake snapshots -> adjusted strategy history -> event account.

Files are registered locally, not selected from the live lake. A manifest is
an index of evidence, never an authorization certificate. Prices re-enter the
existing adjustment gate. Corporate-action coverage and investor-specific
account policy and source action facts are mandatory hash-bound inputs.
Account results are computed from actual fills for every strategy and parameter set.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo
from decimal import Decimal
from hashlib import sha256
import json
import csv
from collections import deque
from pathlib import Path
import numpy as np

from account_allocation import AllocationLimits
from account_history import HistoryDay, RegistrationAction, run_account_history
from adjusted_panel import load_adjusted_lake_panel, _bs_code
from contracts import Bar
from core.contracts import HistoryBar, MarketFrame
from corporate_actions import normalize_dividend_records, reconcile_corporate_actions
from evaluator import evaluate_equity
from panel_features import own_day_rolling_mean
from strategy import canonical_hash, code_hash
from provider_cache import validate_cache
from fees import FeePolicy
from tax_ledger import TaxPolicy, TaxLedger


EXECUTION = {
    'model':'account_history', 'lot_size':100, 'stock_weight':.4,
    'adv_fraction':.01,'liquidity_window':20,'execution':'next_session_open',
    'fees':'execution.ExecutionEngine','account':'account_ledger.AccountLedger',
    'corporate_actions':'distribution_ledger.AccountDistributionLedger',
    'taxes':'actual_daily_net_FIFO_dividend_tax',
}


def _digest(path): return sha256(path.read_bytes()).hexdigest()


class HistoricalLake:
    def __init__(self,root:Path,config:dict):
        self.root=root.resolve(); self.config=config
        self.entries={entry['id']:entry for entry in config['datasets']}
        if len(self.entries)!=len(config['datasets']): raise ValueError('duplicate historical dataset id')

    def datasets(self):
        return [{key:entry[key] for key in ('id','name')} for entry in self.entries.values()]

    def _path(self,root,name):
        path=(root/name).resolve()
        if not path.is_relative_to(root): raise ValueError('historical evidence path escapes snapshot')
        return path

    def _load(self,config,requirements):
        if config['dataset_id'] not in self.entries: raise KeyError('historical_dataset_missing')
        provided={'open','high','low','close','volume','amount','signal_close'}
        if requirements.frequency!='daily' or requirements.price_basis!='adjusted':
            raise ValueError('historical adapter supports daily adjusted signal data only')
        folder=self._path(self.root,self.entries[config['dataset_id']]['path'])
        manifest_path=folder/'manifest.json'; manifest=json.loads(manifest_path.read_bytes())
        if manifest.get('schema')!=2: raise ValueError('unsupported historical snapshot schema')
        overlap=set(manifest.get('fields',{}))&provided
        if overlap: raise ValueError('reserved price fields cannot be redefined: '+', '.join(sorted(overlap)))
        scope=manifest['scope']
        if not isinstance(scope['universe'],str) or not scope['universe'].strip(): raise ValueError('explicit dataset universe scope required')
        if requirements.universe!='dataset' and requirements.universe!=scope['universe']:
            raise ValueError(f'universe_scope_mismatch: 策略要求 {requirements.universe}；数据仅为 {scope["universe"]}，不能替代全市场。')
        extra={name:manifest['fields'][name] for name in set(requirements.fields)-provided
               if name in manifest.get('fields',{})}
        missing=set(requirements.fields)-provided-set(extra)
        if missing: raise ValueError('unsupported_fields: '+', '.join(sorted(missing)))
        for name,spec in extra.items():
            if (set(spec)!={'column','unit'} or any(not isinstance(spec[key],str) or not spec[key].strip() for key in spec)):
                raise ValueError('declared numeric field column and unit required: '+name)

        refs=manifest['files']
        if not isinstance(refs,list) or not refs: raise ValueError('historical evidence files are required')
        hashes={}
        roles={}
        market_files={}
        account_files={}
        for ref in refs:
            path=self._path(folder,ref['file'])
            digest=_digest(path)
            if digest!=ref['sha256']: raise ValueError(f'historical evidence hash mismatch: {ref["file"]}')
            if ref['role'] not in ('market','account'): raise ValueError('evidence role must be market or account')
            if str(path) in hashes: raise ValueError('duplicate historical evidence file')
            hashes[str(path)]=digest
            roles[str(path)]=ref['role']
        def evidence(name):
            path=self._path(folder,name)
            if str(path) not in hashes: raise ValueError(f'unbound historical evidence: {name}')
            if roles[str(path)]=='account': account_files[str(path)]=hashes[str(path)]
            return path
        def market_evidence(name):
            path=evidence(name)
            if roles[str(path)]!='market': raise ValueError(f'market source incorrectly marked as account: {name}')
            market_files[str(path)]=hashes[str(path)]
            return path
        calendar_payload=json.loads(market_evidence(manifest['calendar']).read_bytes())
        calendar=tuple(date.fromisoformat(value) for value in calendar_payload['sessions'])
        # Calendar evidence is mandatory even if OHLC happens to omit a session.
        market_evidence(calendar_payload['source_file'])
        if len(calendar)<2 or tuple(sorted(set(calendar)))!=calendar: raise ValueError('ordered calendar and next-session sentinel required')
        price=manifest['price']
        for directory in ('daily_dir','cache_dir','stock_basic_dir'):
            path=self._path(folder,price[directory])
            for file in path.iterdir():
                if file.is_file(): market_evidence(str(file.relative_to(folder)))
        panel=load_adjusted_lake_panel(
            self._path(folder,price['daily_dir']),self._path(folder,price['cache_dir']),
            factor_start=date.fromisoformat(price['factor_start']),factor_end=date.fromisoformat(price['factor_end']),
            code_changes_path=market_evidence(price['code_changes']),stock_basic_dir=self._path(folder,price['stock_basic_dir']))
        declared=tuple(manifest['scope']['symbols'])
        if requirements.universe!='dataset':
            universe_source=market_evidence(scope['universe_source'])
            universe=json.loads(universe_source.read_bytes())
            if universe['universe']!=requirements.universe or tuple(universe['symbols'])!=declared:
                raise ValueError('universe source security list differs from dataset scope')
            if requirements.symbols is not None and requirements.symbols!=declared:
                raise ValueError('universe_scope_mismatch: requested subset does not satisfy complete universe')

        if tuple(sorted(set(declared)))!=declared or panel.symbols!=declared:
            raise ValueError('declared security scope differs from supplied historical lake')
        if tuple(calendar[:-1])!=panel.calendar:
            raise ValueError('price dates must exactly cover calendar, with explicit next-session sentinel')
        start,end=date.fromisoformat(config['start']),date.fromisoformat(config['end'])
        if not calendar[0]<=start<=end<=calendar[-2]: raise ValueError('requested interval is outside historical snapshot')
        warmup=max(requirements.history_bars,EXECUTION['liquidity_window'])
        if sum(day<start for day in calendar)<warmup:
            raise ValueError(f'insufficient pre-start history: requires {warmup} sessions (strategy and execution)')
        # Each explicit code/year request must be source-backed, including empty
        # results. A successful empty response is evidence; a missing file is not.
        coverage=manifest['corporate_action_sources']; source_events={}; query_keys=set()
        for item in coverage:
            path=market_evidence(item['file']); payload=json.loads(path.read_bytes()); request=payload['request']
            if request['kind']!='dividend': raise ValueError('dividend source request required')
            if request['start'] is not None or request['end'] is not None: raise ValueError('dividend source requires year request')
            validate_cache(payload,request)
            code=request['code'].split('.')[-1]; year=int(request['year'])
            if request['code']!=_bs_code(code): raise ValueError('corporate action provider identity invalid')
            required_fields={'code','dividOperateDate','dividRegistDate','dividPayDate','dividStockMarketDate',
                'dividCashPsBeforeTax','dividStocksPs','dividReserveToStockPs'}
            if not required_fields<=set(payload['raw_response']['fields']): raise ValueError('corporate action source fields incomplete')
            if (code,year) in query_keys: raise ValueError('duplicate corporate action coverage')
            query_keys.add((code,year))
            raw=payload['raw_response']
            records=[dict(zip(raw['fields'],row)) for row in raw['rows']]
            for row,record in enumerate(records):
                event=normalize_dividend_records([record])[0]
                if event.code!=request['code']: raise ValueError('corporate action source code differs from request')
                event=replace(event,code=code)
                if event.registration_date is not None and calendar[0]<=event.registration_date<=end:
                    if not event.executable: raise ValueError('corporate action settlement evidence incomplete')
                    source_events[(item['file'],row)]=(event,hashes[str(path)])
        required={(code,year) for code in declared for year in range(calendar[0].year,end.year+1)}
        if not required<=query_keys: raise ValueError('corporate action per-security/year source coverage incomplete')
        policy_file=evidence(manifest['account_policy'])
        policy=json.loads(policy_file.read_bytes())
        if policy['schema']!=1 or policy['investor']!='domestic_individual_unrestricted_a_share':
            raise ValueError('unsupported investor account policy')
        for source in policy['sources']:
            path=evidence(source['file'])
            if hashes[str(path)]!=source['sha256']: raise ValueError('policy source hash mismatch')
        policy_hash=hashes[str(policy_file)]
        fees=FeePolicy.from_mapping(policy['fees'],policy_hash)
        taxes=TaxPolicy.from_mapping(policy['tax'],policy_hash)
        if start<taxes.effective: raise ValueError('requested start precedes supported tax effective date')
        matched={}; observations=[]; facts={}
        for item in manifest['action_facts']:
            key=(item['source_file'],item['source_row'])
            if key not in source_events:
                # Full snapshot facts remain bound when the requested interval is shorter.
                path=market_evidence(item['source_file'])
                payload=json.loads(path.read_bytes()); raw=payload['raw_response']
                row=item['source_row']
                if type(row) is not int or not 0<=row<len(raw['rows']): raise ValueError('invalid action source row')
                event=replace(normalize_dividend_records([dict(zip(raw['fields'],raw['rows'][row]))])[0],code=payload['request']['code'].split('.')[-1])
                if event.registration_date is None or event.registration_date<calendar[0] or event.registration_date>end: continue
                raise ValueError('unregistered action source')
            if key in matched: raise ValueError('duplicate action fact source')
            event,digest=source_events[key]; matched[key]=item
            source=canonical_hash({'file':item['source_file'],'row':item['source_row'],'hash':digest})
            observations.append((source,item['distribution_id'],event))
            if item['credit_date'] is not None:
                if item['credit_source_file'] is None: raise ValueError('share credit evidence required')
                market_evidence(item['credit_source_file'])
            elif item['credit_source_file'] is not None: raise ValueError('credit source without credit date')
            tax_path=evidence(item['tax_source_file'])
            terms=(item['credit_date'],Decimal(item['stock_nominal_per_share']),Decimal(item['reserve_taxable_per_share']))
            identity=(event.code,item['distribution_id'])
            if identity in facts and facts[identity][0]!=terms: raise ValueError('conflicting action tax/credit facts')
            old=facts.get(identity,(terms,[]))[1]
            facts[identity]=(terms,old+[(digest,hashes[str(tax_path)])])
        if set(matched)!=set(source_events): raise ValueError('corporate action source events missing facts')
        day_inputs={day:[] for day in calendar[:-1]}
        for action in reconcile_corporate_actions(observations,{code:code for code in declared}):
            terms,sources=facts[(action.security_id,action.distribution_id)]
            credit,nominal,reserve=terms
            source_hash=canonical_hash({'observations':[source for source,_ in action.observations],'sources':sources})
            tax_hash=canonical_hash({'sources':sources,'policy':policy_hash})
            TaxLedger(taxes).register(action.distribution_id,action.event,nominal,reserve,tax_hash,
                credit_date=None if credit is None else date.fromisoformat(credit))
            day_inputs[action.event.registration_date].append(RegistrationAction(action.distribution_id,action.event,source_hash,
                None if credit is None else date.fromisoformat(credit),nominal,reserve,tax_hash))
        field_data={}
        if extra:
            import pyarrow as pa
            import pyarrow.parquet as pq
            paths=sorted(self._path(folder,price['daily_dir']).rglob('*.parquet'))
            columns=['code','date',*sorted({spec['column'] for spec in extra.values()})]
            tables=[]
            for path in paths:
                market_evidence(str(path.relative_to(folder)))
                schema=pq.ParquetFile(path).schema_arrow
                for name,spec in extra.items():
                    if spec['column'] not in schema.names:
                        raise ValueError('declared field column missing: '+name)
                    kind=schema.field(spec['column']).type
                    if not (pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)):
                        raise ValueError('declared field column must be numeric: '+name)
                tables.append(pq.ParquetFile(path).read(columns=columns))
            table=pa.concat_tables(tables)
            codes={symbol:i for i,symbol in enumerate(declared)}
            dates={day:j for j,day in enumerate(panel.calendar)}
            field_data={name:np.full(panel.raw_close.shape,np.nan) for name in extra}
            seen=set()
            for row in table.to_pylist():
                code=row['code']; raw_day=row['date']
                day=raw_day if isinstance(raw_day,date) else date.fromisoformat(raw_day)
                if code not in codes or day not in dates: raise ValueError('numeric field row outside declared price scope')
                identity=(code,day)
                if identity in seen: raise ValueError('duplicate numeric field security/date')
                seen.add(identity)
                if requirements.symbols is not None and code not in requirements.symbols: continue
                i,j=codes[code],dates[day]
                for name,spec in extra.items():
                    value=row[spec['column']]
                    if value is None or not np.isfinite(float(value)): raise ValueError(f'numeric field missing/nonfinite: {name}, {code}, {day}')
                    field_data[name][i,j]=float(value)
            for name,values in field_data.items():
                selected=list(range(len(declared))) if requirements.symbols is None else [declared.index(code) for code in requirements.symbols]
                if np.any(np.isfinite(panel.raw_close[selected])&~np.isfinite(values[selected])):
                    raise ValueError('numeric field lacks aligned observed price rows: '+name)
        requested=requirements.symbols
        if requested is not None:
            if not set(requested)<=set(declared): raise ValueError('requested symbols are outside supplied dataset')
            indices=[panel.symbols.index(code) for code in requested]
            field_data={name:values[indices] for name,values in field_data.items()}
            from dataclasses import fields
            arrays={field.name:getattr(panel,field.name)[indices] for field in fields(panel)
                    if isinstance(getattr(panel,field.name),np.ndarray) and getattr(panel,field.name).shape[:1]==(len(declared),)}
            panel=replace(panel,symbols=requested,names=tuple(panel.names[i] for i in indices),
                factor_events=tuple(event for event in panel.factor_events if event.code in requested),
                listing_anchors=tuple(anchor for anchor in panel.listing_anchors if anchor.code in requested),**arrays)
            day_inputs={day:[action for action in actions if action.event.code in requested] for day,actions in day_inputs.items()}
        data_hash=canonical_hash({'files':market_files,'symbols':panel.symbols,'universe':scope['universe'],'price_request':price,'price_basis':'BaoStock-back-factor-signal-index','fields':manifest.get('fields',{})})
        return panel,calendar,{day:tuple(actions) for day,actions in day_inputs.items()},hashes,data_hash,{'policy':policy,'source_hash':policy_hash},canonical_hash({'files':account_files,'facts':manifest['action_facts']}),field_data,scope

    def context(self,config,requirements):
        panel,calendar,_,_,data_hash,policy,account_hash,_,scope=self._load(config,requirements)
        return {'data_scope':scope,'state_start':calendar[0].isoformat(),'price_basis':'BaoStock-back-factor-signal-index',
                'execution':{**EXECUTION,'account_policy':policy},'data_hash':data_hash,'account_hash':account_hash}

    def check(self,config,requirements):
        try:self._load(config,requirements)
        except KeyError as exc:
            code='historical_dataset_missing' if exc.args[0]=='historical_dataset_missing' else 'historical_input_missing'
            message='尚未登记可用的历史快照；请先提供原始行情、复权来源、交易日历、公司行动及账户税费输入。' if code=='historical_dataset_missing' else f'历史快照缺少必需字段或证据：{exc.args[0]}'
            return [{'code':code,'message':message}]
        except Exception as exc:
            return [{'code':'historical_input_invalid','message':f'{type(exc).__name__}: {exc}'}]
        return []

    def run(self,config,strategy,run_id):
        panel,calendar,account_inputs,hashes,data_hash,policy,account_hash,field_data,scope=self._load(config,strategy.requirements())
        start,end=date.fromisoformat(config['start']),date.fromisoformat(config['end'])
        end_index=max(index for index,day in enumerate(calendar[:-1]) if day<=end)
        run_calendar=calendar[:end_index+2]
        # Execution liquidity remains a separately declared 20-own-day rule.
        liquidity=own_day_rolling_mean(panel.amount,20,panel.traded_bar)
        request=strategy.requirements()
        required_history=request.history_bars
        own_history={symbol:deque(maxlen=required_history) for symbol in panel.symbols}
        session_history={symbol:deque(maxlen=required_history) for symbol in panel.symbols}
        last_marks={}
        last_liquidity={}
        def records():
          for j,day in enumerate(run_calendar[:-1]):
            at=datetime.combine(day,time(15),tzinfo=ZoneInfo('Asia/Shanghai'))
            fields={}; bars={}; opening={}; closing={}; previous={}; opening_adv={}; closing_adv={}
            for i,symbol in enumerate(panel.symbols):
                values={name:float(array[i,j]) if np.isfinite(array[i,j]) else None for name,array in (
                    ('open',panel.raw_open),('high',panel.raw_high),('low',panel.raw_low),('close',panel.raw_close),
                    ('volume',panel.volume),('amount',panel.amount),('signal_close',panel.signal_close))}
                values.update({name:float(array[i,j]) if np.isfinite(array[i,j]) else None for name,array in field_data.items()})
                requested_values={name:values[name] for name in request.fields}
                row=HistoryBar(at,requested_values)
                session_history[symbol].append(row)
                if panel.traded_bar[i,j]: own_history[symbol].append(row)
                fields[symbol]=requested_values
                # The strict panel rejects unexplained missing states. Missing
                # execution marks cannot be turned into zero-price observations.
                if any(values[name] is None for name in ('open','high','low','close','volume','amount')):
                    continue
                bars[symbol]=Bar(symbol,day,*(values[name] for name in ('open','high','low','close','volume','amount')))
                opening[symbol]=Decimal(str(values['open'])); closing[symbol]=Decimal(str(values['close']))
                previous[symbol]=last_marks.get(symbol)
                if symbol in last_liquidity: opening_adv[symbol]=last_liquidity[symbol]
                if np.isfinite(liquidity[i,j]): closing_adv[symbol]=Decimal(str(liquidity[i,j]))
            inputs=account_inputs[day]
            frame=MarketFrame(at,'CN_EQUITY',fields,{'next_session':calendar[j+1].isoformat()},
                              {symbol:tuple(sorted({row.at:row for row in (*own_history[symbol],*session_history[symbol])}.values(),key=lambda row:row.at)) for symbol in panel.symbols})
            yield HistoryDay(day,frame,bars,previous,opening,closing,opening_adv,closing_adv,
                calendar[j-1] if j else date.min,inputs)
            last_marks.clear(); last_marks.update({symbol:float(value) for symbol,value in closing.items()}); last_liquidity.clear(); last_liquidity.update(closing_adv)
        result=run_account_history(strategy,calendar=run_calendar,days=records(),
                                   initial_cash=Decimal(str(config['initial_cash'])),trade_start=start,
                                   fees=FeePolicy.from_mapping(policy['policy']['fees'],policy['source_hash']),
                                   tax_policy=TaxPolicy.from_mapping(policy['policy']['tax'],policy['source_hash']))
        points=[(point.day,float(point.equity)) for point in result.equity if point.day>=start]
        metrics=evaluate_equity(points)
        from plugins.recorders.jsonl import _json_value
        output=self.root/self.config['output']/run_id
        if not output.resolve().is_relative_to(self.root): raise ValueError('historical output escapes project')
        output.mkdir(parents=True,exist_ok=False)
        execution_hash=canonical_hash({'rules':EXECUTION,'account_policy':policy,'account_hash':account_hash,'code_hash':code_hash()})
        manifest={'run_id':run_id,'strategy_id':strategy.strategy_id,'status':'input_checked',
            'scope':{'universe':scope['universe'],'symbols':list(panel.symbols),'start':points[0][0].isoformat(),'end':points[-1][0].isoformat()},
            'limitations':['限定登记快照股票池；来源哈希与输入检查不证明供应商完整性。'],
            'effective_config':config,'source_hashes':hashes,'data_hash':data_hash,'code_hash':code_hash(),
            'execution_hash':execution_hash,'account_hash':account_hash,'state_start':calendar[0].isoformat(),
            'parameters':asdict(strategy.params),'config_hash':canonical_hash(config)}
        (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        (output/'metrics.json').write_text(json.dumps(metrics,allow_nan=False,indent=2))
        (output/'trace.json').write_text(json.dumps(_json_value(result),ensure_ascii=False,allow_nan=False))
        trades=_json_value(result.fills)
        audit=output/'audit'; audit.mkdir()
        with (audit/'equity.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=('trade_date','equity')); writer.writeheader()
            writer.writerows({'trade_date':day.isoformat(),'equity':value} for day,value in points)
        with (audit/'fills.csv').open('w',newline='') as stream:
            names=list(trades[0]) if trades else ['order_id','symbol','side','trade_date','shares','price','status']
            writer=csv.DictWriter(stream,fieldnames=names); writer.writeheader(); writer.writerows(trades)
        identity=sha256(str((output/'manifest.json').relative_to(self.root)).encode()).hexdigest()

        evidence=[{'file':str(path.relative_to(self.root)),'sha256':_digest(path)} for path in sorted(output.rglob('*')) if path.is_file()]
        return {'id':identity,'run_id':run_id,'strategy_id':strategy.strategy_id,'status':'input_checked',
            'qualification':'unverified','limitations':manifest['limitations'],'metrics':metrics,
            'manifest_file':str((output/'manifest.json').relative_to(self.root)),
            'manifest_sha256':_digest(output/'manifest.json'),'metrics_sha256':_digest(output/'metrics.json'),
            'source_hashes':hashes,'config_hash':manifest['config_hash'],'data_hash':data_hash,'code_hash':manifest['code_hash'],
            'execution_hash':execution_hash,'note':'复权和逐笔账户输入已检查；限定股票池，来源完整性仍需审核。',
            'manifest':manifest,'trades':trades[:50],'trades_total':len(trades),
            'equity':[{'at':day.isoformat(),'equity':value} for day,value in points],
            'equity_sampling':{'total':len(points),'stride':1},'evidence':evidence,'offset':0,'limit':50}
