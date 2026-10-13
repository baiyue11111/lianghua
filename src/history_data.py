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
from collections import deque
from pathlib import Path
import numpy as np

from core.calculation import AllocationLimits
from collect.data import load_adjusted_lake_panel, _bs_code
from protocal import Bar, DataPort, Dataset, HistoryBar, HistoryDay, MarketFrame, RegistrationAction
from core.calculation import normalize_dividend_records, reconcile_corporate_actions
from core.calculation import Calculation
from strategy import canonical_hash
from provider_cache import validate_cache
from core.calculation import FeePolicy
from core.calculation import TaxPolicy, TaxLedger


LIQUIDITY_WINDOW = 20

class HistoricalLake:
    def __init__(self,data:DataPort,config:dict):
        self.data=data; self.config=config
        self.entries={entry['id']:entry for entry in config['datasets']}
        if len(self.entries)!=len(config['datasets']): raise ValueError('duplicate historical dataset id')

    def datasets(self):
        return [{key:entry[key] for key in ('id','name')} for entry in self.entries.values()]

    def query(self, query):
        from types import SimpleNamespace
        config = {
            'dataset_id': query.dataset,
            'start': query.start.isoformat(),
            'end': query.end.isoformat(),
            'as_of': None if query.as_of is None else query.as_of.isoformat(),
        }
        requirements = SimpleNamespace(
            frequency=query.frequency, price_basis=query.price_basis,
            universe=query.universe, symbols=query.symbols,
            fields=query.fields, history_bars=query.history,
        )
        return self._prepare_dataset(config, requirements)

    def build_query(self, config, requirements):
        from protocal import DataQuery, required_fields
        fields = tuple(config.get('fields') or requirements.fields)
        if not set(requirements.fields) <= set(fields):
            raise ValueError('custom fields omit strategy requirements')
        history = requirements.history_bars if config.get('history', 0) == 0 else config['history']
        if history < requirements.history_bars:
            raise ValueError('custom history is shorter than strategy requirements')
        symbols = config.get('symbols') if config.get('symbols') is not None else requirements.symbols
        if requirements.symbols is not None and tuple(symbols or ()) != tuple(requirements.symbols):
            raise ValueError('custom symbols conflict with strategy scope')
        universe = config.get('universe', 'dataset')
        if requirements.universe != 'dataset' and universe != 'dataset' and universe != requirements.universe:
            raise ValueError('custom universe cannot lower or replace strategy universe')
        universe = requirements.universe if universe == 'dataset' else universe
        return DataQuery(
            dataset=config['dataset_id'],
            start=date.fromisoformat(config['start']), end=date.fromisoformat(config['end']),
            fields=required_fields(fields), history=history,
            price_basis=config.get('price_basis', getattr(requirements, 'price_basis', 'adjusted')),
            as_of=None if config.get('as_of') is None else date.fromisoformat(config['as_of']),
            symbols=None if symbols is None else tuple(symbols), universe=universe,
            frequency=requirements.frequency,
        )

    def _path(self,root,name):
        path=(root/name).resolve()
        if not path.is_relative_to(root): raise ValueError('historical evidence path escapes snapshot')
        return path

    def _load(self,config,requirements):
        if config['dataset_id'] not in self.entries: raise KeyError('historical_dataset_missing')
        provided={'open','high','low','close','volume','amount','signal_close'}
        if requirements.frequency!='daily' or requirements.price_basis!='adjusted':
            raise ValueError('historical adapter supports daily adjusted signal data only')
        dataset_id=config['dataset_id']
        folder=self.data.path('sources',dataset_id,'manifest.json').parent
        def read(name):
            path=self._path(folder,name)
            return self.data.read('sources',dataset_id,path.relative_to(folder).as_posix())
        def refs_under(directory):
            prefix=directory.rstrip('/')+'/'
            return tuple(ref for ref in self.data.list('sources',dataset_id)
                         if ref.name.startswith(prefix))
        manifest=self.data.snapshot(dataset_id)
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
            digest=sha256(read(ref['file'])).hexdigest()
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
        market_evidence(manifest['calendar'])
        calendar_payload=json.loads(read(manifest['calendar']))
        calendar=tuple(date.fromisoformat(value) for value in calendar_payload['sessions'])
        # Calendar evidence is mandatory even if OHLC happens to omit a session.
        calendar_source_name = calendar_payload['source_file']
        market_evidence(calendar_source_name)
        calendar_source = json.loads(read(calendar_source_name))
        coverage_request = calendar_source.get('request')
        if not isinstance(coverage_request, dict) or not isinstance(coverage_request.get('start'), str):
            raise ValueError('calendar source must declare its requested start')
        try:
            calendar_start = date.fromisoformat(coverage_request['start'])
        except ValueError as error:
            raise ValueError('calendar source requested start is invalid') from error
        if calendar_start > calendar[0]:
            raise ValueError('calendar source coverage starts after its first session')
        if len(calendar)<2 or tuple(sorted(set(calendar)))!=calendar: raise ValueError('ordered calendar and next-session sentinel required')
        price=manifest['price']
        for directory in ('daily_dir','cache_dir','stock_basic_dir'):
            for ref in refs_under(price[directory]):
                market_evidence(ref.name)
        panel=load_adjusted_lake_panel(
            self._path(folder,price['daily_dir']),self._path(folder,price['cache_dir']),
            factor_start=date.fromisoformat(price['factor_start']),factor_end=date.fromisoformat(price['factor_end']),
            code_changes_path=market_evidence(price['code_changes']),stock_basic_dir=self._path(folder,price['stock_basic_dir']),
            read=lambda path: read(path.relative_to(folder).as_posix()),
            list_files=lambda directory, pattern: tuple(
                self.data.path('sources', dataset_id, ref.name)
                for ref in self.data.list('sources', dataset_id)
                if ref.name.startswith(directory.relative_to(folder).as_posix().rstrip('/') + '/')
                and ref.name.endswith(pattern.removeprefix('*'))))
        declared=tuple(scope['symbols'])
        if requirements.universe!='dataset':
            if scope['universe']!=requirements.universe:
                raise ValueError('universe_scope_mismatch: dataset scope does not satisfy the requested universe')
            if requirements.symbols is not None and requirements.symbols!=declared:
                raise ValueError('universe_scope_mismatch: requested subset does not satisfy complete universe')

        scope = dict(scope)
        scope['calendar_start'] = calendar_start.isoformat()
        scope['calendar_end'] = calendar[-2].isoformat()
        if tuple(sorted(set(declared)))!=declared or panel.symbols!=declared:
            raise ValueError('declared security scope differs from supplied historical lake')
        if tuple(calendar[:-1])!=panel.calendar:
            raise ValueError('price dates must exactly cover calendar, with explicit next-session sentinel')
        start,end=date.fromisoformat(config['start']),date.fromisoformat(config['end'])
        as_of=None if config.get('as_of') is None else date.fromisoformat(config['as_of'])
        if as_of is not None and as_of < end:
            raise ValueError('data as_of precedes query end')
        if as_of is not None:
            availability = manifest.get('source_availability')
            if not isinstance(availability, list) or not availability:
                raise ValueError('point-in-time query requires unified source availability evidence')
            by_file = {}
            for item in availability:
                if (not isinstance(item, dict) or set(item) != {'file', 'sha256', 'available_at'}
                        or item['file'] in by_file):
                    raise ValueError('invalid unified source availability evidence')
                by_file[item['file']] = item
            for absolute, digest in hashes.items():
                name = str(Path(absolute).relative_to(folder))
                item = by_file.get(name)
                if item is None or item['sha256'] != digest:
                    raise ValueError(f'point-in-time source availability is not bound: {name}')
                try:
                    available_day = date.fromisoformat(item['available_at'])
                except (TypeError, ValueError) as error:
                    raise ValueError(f'invalid source availability date: {name}') from error
                if available_day > as_of:
                    raise ValueError(f'source evidence became available after query cutoff: {name}')
        if as_of is not None and date.fromisoformat(price['factor_end']) != as_of:
            raise ValueError('point-in-time factor snapshot must end exactly at query cutoff')
        if as_of is not None:
            factor_files = [ref.name for ref in refs_under(price['cache_dir'])]
            if not factor_files:
                raise ValueError('point-in-time query requires factor availability evidence')
            for name in factor_files:
                payload = json.loads(read(name))
                if not isinstance(payload, dict):
                    raise ValueError(f'invalid factor evidence: {name}')
        if not calendar[0]<=start<=end<=calendar[-2]: raise ValueError('requested interval is outside historical snapshot')
        warmup=max(requirements.history_bars,LIQUIDITY_WINDOW)
        if sum(day<start for day in calendar)<warmup:
            raise ValueError(f'insufficient pre-start history: requires {warmup} sessions (strategy and execution)')
        # Each explicit code/year request must be source-backed, including empty
        # results. A successful empty response is evidence; a missing file is not.
        coverage=manifest['corporate_action_sources']; source_events={}; query_keys=set()
        for item in coverage:
            path=market_evidence(item['file']); payload=json.loads(read(item['file'])); request=payload['request']
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
        policy=json.loads(read(manifest['account_policy']))
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
                payload=json.loads(read(item['source_file'])); raw=payload['raw_response']
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
            refs=tuple(ref for ref in refs_under(price['daily_dir']) if ref.name.endswith('.parquet'))
            columns=['code','date',*sorted({spec['column'] for spec in extra.values()})]
            tables=[]
            for ref in refs:
                market_evidence(ref.name)
                parquet=pq.ParquetFile(pa.BufferReader(self.data.read_ref(ref)))
                schema=parquet.schema_arrow
                for name,spec in extra.items():
                    if spec['column'] not in schema.names:
                        raise ValueError('declared field column missing: '+name)
                    kind=schema.field(spec['column']).type
                    if not (pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)):
                        raise ValueError('declared field column must be numeric: '+name)
                tables.append(parquet.read(columns=columns))
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

    def context(self, dataset):
        scope = dict(dataset.scope)
        if 'symbols' in scope:
            scope['symbols'] = list(scope['symbols'])
        return {'data_scope':scope,'state_start':dataset.calendar[0].isoformat(),
                'price_basis':'BaoStock-back-factor-signal-index',
                'liquidity_window': LIQUIDITY_WINDOW,
                **dict(dataset.provenance)}

    @staticmethod
    def check(dataset):
        if dataset is None:
            return [{'code': 'historical_input_invalid', 'message': '历史查询没有返回 Dataset。'}]
        return []

    def _prepare_dataset(self,config,requirements):
        from protocal import Dataset
        panel,calendar,account_inputs,hashes,data_hash,policy,account_hash,field_data,scope=self._load(config,requirements)
        start,end=date.fromisoformat(config['start']),date.fromisoformat(config['end'])
        as_of=None if config.get('as_of') is None else date.fromisoformat(config['as_of'])
        end_index=max(index for index,day in enumerate(calendar[:-1]) if day<=end)
        run_calendar=calendar[:end_index+2]
        # Execution liquidity remains a separately declared 20-own-day rule.
        amount_liquidity=Calculation.own_day_rolling_mean(panel.amount,LIQUIDITY_WINDOW,panel.traded_bar)
        volume_liquidity=Calculation.own_day_rolling_mean(panel.volume,LIQUIDITY_WINDOW,panel.traded_bar)
        request=requirements
        required_history=request.history_bars
        own_history={symbol:deque(maxlen=required_history) for symbol in panel.symbols}
        session_history={symbol:deque(maxlen=required_history) for symbol in panel.symbols}
        last_marks={}
        last_liquidity={}
        last_volume={}
        def records():
          for j,day in enumerate(run_calendar[:-1]):
            at=datetime.combine(day,time(15),tzinfo=ZoneInfo('Asia/Shanghai'))
            fields={}; bars={}; opening={}; closing={}; previous={}; opening_adv={}; closing_adv={}; opening_volume20={}; closing_volume20={}
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
                if symbol in last_volume: opening_volume20[symbol]=last_volume[symbol]
                if np.isfinite(amount_liquidity[i,j]): closing_adv[symbol]=Decimal(str(amount_liquidity[i,j]))
                if np.isfinite(volume_liquidity[i,j]): closing_volume20[symbol]=Decimal(str(volume_liquidity[i,j]))
            inputs=account_inputs[day]
            frame=MarketFrame(at,'CN_EQUITY',fields,{'next_session':calendar[j+1].isoformat()},
                              {symbol:tuple(sorted({row.at:row for row in (*own_history[symbol],*session_history[symbol])}.values(),key=lambda row:row.at)) for symbol in panel.symbols})
            yield HistoryDay(day,frame,bars,previous,opening,closing,opening_adv,closing_adv,
                calendar[j-1] if j else date.min,inputs,opening_volume20)
            last_marks.clear(); last_marks.update({symbol:float(value) for symbol,value in closing.items()})
            last_liquidity.update(closing_adv)
            last_volume.update(closing_volume20)
        prepared = tuple(records())
        plugins = config.get('signal_plugins', self.config.get('signal_plugins', {}))
        plugin_provenance = None
        if plugins:
            prepared, plugin_provenance = self._apply_signal_plugins(prepared, plugins, start, end)
        provenance = {
            'source_hashes': hashes, 'data_hash': data_hash, 'account_hash': account_hash,
            'state_start': calendar[0].isoformat(),
            'as_of': None if as_of is None else as_of.isoformat(),
            'query_semantics': 'point_in_time' if as_of is not None else 'snapshot_replay',
        }
        if plugin_provenance is not None:
            provenance['signal_plugins'] = plugin_provenance
        return Dataset(
            config['dataset_id'], prepared,
            {'fees': FeePolicy.from_mapping(policy['policy']['fees'], policy['source_hash']),
             'tax_policy': TaxPolicy.from_mapping(policy['policy']['tax'], policy['source_hash'])},
            provenance,
            {**scope, 'symbols': tuple(panel.symbols)}, tuple(run_calendar),
            requirements.fields, requirements.history_bars,
        )

    @staticmethod
    def _apply_signal_plugins(records, plugins, start, end):
        from dataclasses import replace
        from protocal import HistoryBar, MarketFrame
        from importlib import import_module
        from plugins.data import Adapter
        from strategy import canonical_hash

        def names(value, label):
            value = tuple(value)
            if (not value or len(set(value)) != len(value)
                    or any(not isinstance(field, str) or not field for field in value)):
                raise ValueError(f'{label} must be unique non-empty names')
            return value

        def projection(record, selected):
            fields = {}
            for symbol, values in record.frame.fields.items():
                if any(field not in values for field in selected):
                    raise ValueError('signal plugin field is missing from strategy input')
                fields[symbol] = {field: values[field] for field in selected}
            history = {}
            for symbol, rows in record.frame.history.items():
                history[symbol] = tuple(
                    HistoryBar(row.at, {field: row.fields[field] for field in selected})
                    for row in rows
                )
            return replace(record.frame, fields=fields, history=history)

        def checked_output(original, output, selected):
            output = tuple(output)
            if len(output) != len(original):
                raise ValueError('signal plugin output length changed')
            for before, after in zip(original, output):
                if not isinstance(after, MarketFrame) or after.at != before.at:
                    raise ValueError('signal plugin output timestamp changed')
                if tuple(after.fields) != tuple(before.fields):
                    raise ValueError('signal plugin output symbols changed')
                for symbol in before.fields:
                    if tuple(after.fields[symbol]) != selected:
                        raise ValueError('signal plugin output fields changed')
                if tuple(after.history) != tuple(before.history):
                    raise ValueError('signal plugin output history symbols changed')
                for symbol in before.history:
                    before_times = tuple(row.at for row in before.history[symbol])
                    after_times = tuple(row.at for row in after.history[symbol])
                    if after_times != before_times:
                        raise ValueError('signal plugin output history timestamps changed')
                    current = next((row for row in after.history[symbol] if row.at == after.at), None)
                    if current is not None and dict(current.fields) != dict(after.fields[symbol]):
                        raise ValueError('signal plugin output current frame/history differs')
            Adapter.validate(output, fields=selected)
            return output

        def merge(original, transformed, selected):
            output = []
            for record, frame in zip(original, transformed):
                fields = {symbol: dict(values) for symbol, values in record.frame.fields.items()}
                for symbol in fields:
                    fields[symbol].update(frame.fields[symbol])
                history = {}
                for symbol, rows in record.frame.history.items():
                    if tuple(row.at for row in rows) != tuple(row.at for row in frame.history[symbol]):
                        raise ValueError('signal plugin merge would discard history rows')
                    changed = {row.at: row for row in frame.history[symbol]}
                    history[symbol] = tuple(replace(row, fields={**row.fields, **changed[row.at].fields})
                                            for row in rows)
                output.append(replace(record, frame=replace(record.frame, fields=fields, history=history)))
            return tuple(output)

        if not isinstance(plugins, dict) or set(plugins) - {'adapter', 'scaler'}:
            raise ValueError('unsupported signal plugin declaration')
        provenance = {}
        adapter_spec = plugins.get('adapter')
        if adapter_spec is not None:
            if (not isinstance(adapter_spec, dict)
                    or set(adapter_spec) - {'module', 'method', 'fields', 'params'}
                    or adapter_spec.get('method') not in {'validate', 'align'}
                    or not adapter_spec.get('fields')):
                raise ValueError('signal adapter requires module, method, fields, and params')
            module_name = adapter_spec.get('module')
            if not isinstance(module_name, str) or ':' not in module_name:
                raise ValueError('signal adapter module must be explicit')
            module, name = adapter_spec['module'].split(':', 1)
            adapter = getattr(import_module(module), name)
            method = getattr(adapter, adapter_spec['method'], None)
            if not callable(method):
                raise ValueError('signal adapter method is not callable')
            selected_fields = names(adapter_spec['fields'], 'signal adapter fields')
            params = dict(adapter_spec.get('params', {}))
            signal_frames = tuple(projection(record, selected_fields) for record in records)
            if adapter_spec['method'] == 'align':
                transformed = method(signal_frames, tuple(frame.at for frame in signal_frames), **params)
            else:
                transformed = method(signal_frames, **params)
            transformed = checked_output(signal_frames, transformed, selected_fields)
            records = merge(records, transformed, selected_fields)
            provenance['adapter'] = {
                'module': module_name, 'method': adapter_spec['method'],
                'fields': list(selected_fields), 'params': params,
                'output_hash': canonical_hash([
                    {'at': frame.at.isoformat(), 'fields': {
                        symbol: dict(values) for symbol, values in frame.fields.items()
                    }} for frame in transformed
                ]),
            }
        spec = plugins.get('scaler')
        if spec is None:
            return records, provenance
        if (not isinstance(spec, dict)
                or set(spec) - {'module', 'fields', 'fit_start', 'fit_end', 'params'}):
            raise ValueError('invalid signal scaler declaration')
        if (not isinstance(spec.get('module'), str) or ':' not in spec['module']
                or not spec.get('fields') or 'fit_start' not in spec or 'fit_end' not in spec):
            raise ValueError('signal scaler requires module, fields, fit_start, and fit_end')
        selected_fields = names(spec['fields'], 'signal scaler fields')
        fit_start = date.fromisoformat(spec['fit_start'])
        fit_end = date.fromisoformat(spec['fit_end'])
        if fit_start > fit_end or fit_end >= start:
            raise ValueError('signal scaler training window must end before backtest start')
        module, name = spec['module'].split(':', 1)
        scaler_type = getattr(import_module(module), name)
        scaler = scaler_type(**dict(spec.get('params', {})))
        if not all(callable(getattr(scaler, name, None)) for name in ('fit', 'transform', 'statistics')):
            raise ValueError('signal scaler must implement fit, transform, and statistics')
        train = [record for record in records if fit_start <= record.trade_date <= fit_end]
        if (not train or train[0].trade_date != fit_start
                or train[-1].trade_date != fit_end):
            raise ValueError('signal scaler training window is not fully covered by records')
        symbols = tuple(train[0].frame.fields)
        matrix = []
        for record in train:
            for symbol in symbols:
                values = record.frame.fields[symbol]
                if any(field not in values for field in selected_fields):
                    raise ValueError('signal scaler field is missing from strategy input')
                matrix.append([values[field] for field in selected_fields])
        scaler.fit(matrix)
        statistics = scaler.statistics()
        if not isinstance(statistics, dict):
            raise ValueError('signal scaler statistics must be a mapping')
        statistics = {key: np.asarray(value).tolist() for key, value in statistics.items()}
        flat_statistics = [item for value in statistics.values()
                           for item in (value if isinstance(value, list) else [value])]
        if not np.isfinite(np.asarray(flat_statistics, dtype=float)).all():
            raise ValueError('signal scaler statistics must be finite')

        def transform(values):
            output = scaler.transform(values)
            try:
                raw = np.asarray(output, dtype=object)
            except (TypeError, ValueError) as error:
                raise ValueError('signal scaler transform must return a numeric matrix') from error
            if raw.ndim != 2 or raw.shape != (len(values), len(selected_fields)):
                raise ValueError('signal scaler transform shape does not match input')
            result = []
            for row in raw.tolist():
                converted = []
                for value in row:
                    if isinstance(value, (bool, np.bool_)):
                        raise ValueError('signal scaler transform must return real finite values')
                    try:
                        number = float(value)
                    except (TypeError, ValueError, OverflowError) as error:
                        raise ValueError('signal scaler transform must return real finite values') from error
                    if not np.isfinite(number):
                        raise ValueError('signal scaler transform must return real finite values')
                    converted.append(number)
                result.append(converted)
            return result

        output = []
        for record in records:
            fields = {symbol: dict(values) for symbol, values in record.frame.fields.items()}
            history = {}
            for symbol, rows in record.frame.history.items():
                history[symbol] = tuple(
                    replace(item, fields={
                        **item.fields,
                        **dict(zip(selected_fields, transform(
                            [[item.fields[field] for field in selected_fields]]
                        )[0]))
                    })
                    for item in rows)
            for symbol in symbols:
                fields[symbol].update(dict(zip(
                    selected_fields,
                    transform([[fields[symbol][field] for field in selected_fields]])[0],
                )))
            output.append(replace(record, frame=replace(record.frame, fields=fields, history=history)))
        provenance['scaler'] = {
            'module': spec['module'], 'fields': list(selected_fields),
            'fit_start': fit_start.isoformat(), 'fit_end': fit_end.isoformat(),
            'params': dict(spec.get('params', {})), 'statistics': statistics,
            'statistics_hash': canonical_hash(statistics),
        }
        return tuple(output), provenance
