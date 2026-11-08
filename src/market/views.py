"""Read-only data views behind the standard backend port."""
from collections.abc import Mapping
from datetime import date, datetime

from protocol import DataError, EXECUTION_FIELDS, SQLQuery


class Views:
    def __init__(self, data, sources):
        self.data = data
        self.sources = sources

    def handle(self, operation, value):
        if operation == "data.status":
            return self.profile_report()
        if operation == "data.research_inputs":
            return self.research_report()
        if operation == "data.search":
            return self.search(value["q"])
        if operation == "data.bars":
            return self.bars(value)
        if operation == "data.check":
            return self.doctor_report()
        raise ValueError(f"unsupported data operation: {operation}")

    def _tables(self, data_type):
        sources = self.data.registry()['sources']
        return tuple(dict.fromkeys(
            self.data.source(source.get('storage_id', source['id']),
                             'bars' if data_type == 'daily' else 'records')
            for source in sources if source.get('data_type') == data_type))

    @staticmethod
    def _name(name):
        return '"' + name.replace('"', '""') + '"'

    def _source(self, tables):
        for reference in tables:
            if not isinstance(reference, str) or not reference.startswith('lake:'):
                raise DataError('invalid_ref', 'native table reference required')
        return '(' + ' UNION ALL BY NAME '.join(
            'SELECT * FROM ' + self._name(reference[5:]) for reference in tables) + ')'

    def _rows(self, sql, values=()):
        return self.data.scan(SQLQuery(sql, tuple(values))).table.to_pylist()

    def _columns(self, source):
        return set(self.data.scan(SQLQuery('SELECT * FROM ' + source + ' LIMIT 0')).table.column_names)

    def _column(self, name, columns):
        return self._name(name) if name in columns else 'NULL'

    def _table_facts(self, tables, date_field, *, with_fields=True):
        fact = {'rows': 0, 'symbols': 0, 'first_date': None, 'last_date': None,
                'columns': set(), 'field_stats': {}, 'missing': not tables}
        if not tables:
            return fact
        source = self._source(tables)
        columns = self._columns(source)
        code, dates = self._column('code', columns), self._column(date_field, columns)
        expressions = [f'count(*) AS rows', f'count(DISTINCT {code}) AS symbols',
                       f'min({dates}) AS first_date', f'max({dates}) AS last_date']
        names = sorted(columns) if with_fields else ()
        for index, name in enumerate(names):
            field = self._name(name)
            expressions.extend((f'count({field}) AS n{index}',
                f'100.0 * avg(CASE WHEN {field} IS NOT NULL THEN 1.0 ELSE 0.0 END) AS c{index}',
                f'count(DISTINCT CASE WHEN {field} IS NOT NULL THEN {code} END) AS s{index}'))
        if 'status' in columns:
            expressions.append("count(*) FILTER (WHERE lower(cast(status AS VARCHAR)) IN ('listed','现存','true')) AS listed")
        row = self._rows('SELECT ' + ', '.join(expressions) + ' FROM ' + source)[0]
        fact.update(rows=row['rows'], symbols=row['symbols'], columns=columns,
                    first_date=None if row['first_date'] is None else str(row['first_date']),
                    last_date=None if row['last_date'] is None else str(row['last_date']))
        fact['field_stats'] = {name: {'non_null': row[f'n{index}'], 'coverage': row[f'c{index}'],
                                     'symbols': row[f's{index}'] if 'code' in columns else None}
                               for index, name in enumerate(names)}
        if 'status' in columns:
            fact.update(listed=row['listed'], delisted_or_unknown=row['rows'] - row['listed'])
        return fact

    def _daily_fact(self, tables):
        fact = self._table_facts(tables, 'date', with_fields=False)
        fact.update(duplicate_keys=0, invalid_price_rows=0,
                    missing_fields={name: 0 for name in EXECUTION_FIELDS},
                    field_coverage={name: None for name in EXECUTION_FIELDS},
                    field_symbols={name: 0 for name in EXECUTION_FIELDS}, adjusted_close_rows=0,
                    trade_state_issues={'suspended_with_turnover': 0, 'trading_without_turnover': 0,
                                        'unknown_zero_turnover': 0},
                    latest_date=None, latest_stock_count=0, tracked_stock_count=0,
                    median_stock_date=None, yearly={}, price_basis_issues=[])
        if not tables:
            return fact
        source, columns = self._source(tables), fact['columns']
        expressions = ['count(*) - count(DISTINCT (code, date)) AS duplicates']
        for index, name in enumerate(EXECUTION_FIELDS):
            field = self._column(name, columns)
            expressions.extend((f'count({field}) AS n{index}',
                f'100.0 * avg(CASE WHEN {field} IS NOT NULL THEN 1.0 ELSE 0.0 END) AS c{index}',
                f'count(DISTINCT CASE WHEN {field} IS NOT NULL THEN code END) AS s{index}'))
        prices = ('open', 'high', 'low', 'close')
        invalid = ' OR '.join(f'{self._name(name)} IS NULL OR {self._name(name)} <= 0' for name in prices)
        invalid = invalid + ' OR high < low' if set(prices) <= columns else 'false'
        expressions.append(f'count(*) FILTER (WHERE {invalid}) AS invalid')
        status, volume, amount = (self._column(name, columns) for name in ('tradestatus', 'volume', 'amount'))
        expressions.extend((
            f'count(*) FILTER (WHERE {status}=0 AND (coalesce({volume},0)>0 OR coalesce({amount},0)>0)) AS suspended',
            f'count(*) FILTER (WHERE {status}=1 AND (coalesce({volume},0)<=0 OR coalesce({amount},0)<=0)) AS trading',
            f'count(*) FILTER (WHERE {status} IS NULL AND coalesce({volume},0)=0 AND coalesce({amount},0)=0) AS unknown',
            f"count({self._column('close_hfq', columns)}) AS adjusted"))
        row = self._rows('SELECT ' + ', '.join(expressions) + ' FROM ' + source)[0]
        for index, name in enumerate(EXECUTION_FIELDS):
            fact['missing_fields'][name] = fact['rows'] - row[f'n{index}']
            fact['field_coverage'][name] = row[f'c{index}'] if name in columns else None
            fact['field_symbols'][name] = row[f's{index}']
        fact.update(duplicate_keys=row['duplicates'], invalid_price_rows=row['invalid'],
                    adjusted_close_rows=row['adjusted'])
        if {'tradestatus', 'volume', 'amount'} <= columns:
            fact['trade_state_issues'] = dict(zip(fact['trade_state_issues'],
                                                  (row['suspended'], row['trading'], row['unknown'])))
        latest = self._rows(f"""WITH latest AS (
            SELECT code, max(cast(date AS DATE)) AS last_date FROM {source} GROUP BY code)
            SELECT max(last_date) AS latest_date, count(*) AS tracked_stock_count,
                count(*) FILTER (WHERE last_date=(SELECT max(last_date) FROM latest)) AS latest_stock_count,
                cast(median(last_date) AS DATE) AS median_stock_date FROM latest""")[0]
        fact.update({name: None if value is None else str(value) if isinstance(value, date) else value
                     for name, value in latest.items()})
        yearly = self._rows(f"""SELECT year(cast(date AS DATE)) AS year,
            count(*) AS rows, count(DISTINCT code) AS symbols FROM {source} GROUP BY 1 ORDER BY 1""")
        fact['yearly'] = {str(row['year']): {'rows': row['rows'], 'symbols': row['symbols']} for row in yearly}
        if {'code', 'date', 'amount', 'volume', 'low', 'high', 'close'} <= columns:
            rows = self._rows(f"""SELECT code, count(*) AS rows, min(date) AS first_date,
                max(date) AS last_date, median(amount/volume/close) AS median_vwap_close_ratio
                FROM {source} WHERE volume>0 AND amount>0 AND low>0 AND high>0 AND close>0
                AND (amount/volume < low*0.99 OR amount/volume > high*1.01)
                GROUP BY code ORDER BY rows DESC, code""")
            fact['price_basis_issues'] = [{**row, 'first_date': str(row['first_date']),
                                          'last_date': str(row['last_date'])} for row in rows]
        return fact

    def _fingerprint(self, reference):
        # Hash ordered row digests inside the DBMS; only the final digest leaves it.
        order = ', '.join(self._name(name) for name in self.data.definition(reference[5:]).keys)
        source = self._source((reference,))
        return self._rows(f"""SELECT sha256(coalesce(string_agg(sha256(to_json(r)), '' ORDER BY {order}), '')) AS sha256
            FROM {source} r""")[0]['sha256']

    def source_facts(self):
        with self.data.transaction(readonly=True):
            daily_tables = self._tables('daily')
            tables = {kind: self._tables(kind) for kind in ('stock_info', 'valuation', 'fundamentals')}
            facts = {kind: self._table_facts(value, 'quarter') for kind, value in tables.items()}
            counts = self._rows("""SELECT count(*) AS files,
                count(*) FILTER (WHERE kind='adjust') AS factors FROM fact_coverage
                WHERE kind IN ('adjust','dividend','security')""")[0]
            coverage = self._rows("""SELECT reference FROM (
                SELECT reference FROM fact_coverage WHERE kind IN ('adjust','dividend','security')
                ORDER BY reference LIMIT 8) UNION SELECT reference FROM (
                SELECT reference FROM fact_coverage WHERE kind IN ('adjust','dividend','security')
                ORDER BY reference DESC LIMIT 8) ORDER BY reference""")
            valid = invalid = 0
            for row in coverage:
                try:
                    self.data.object(row['reference'])
                    valid += 1
                except (KeyError, TypeError, ValueError):
                    invalid += 1
            changes, checked, change_refs = [], True, set()
            datasets = self.data.registry()['datasets']
            for descriptor in datasets:
                manifest = self.data.manifest(descriptor['id'])
                reference = manifest.get('inputs', {}).get('code_changes')
                if reference is None:
                    checked = False
                    continue
                change_refs.add(reference)
            for reference in sorted(change_refs):
                value = self.data.object(reference)
                if not isinstance(value, dict) or not isinstance(value.get('events'), list):
                    raise DataError('invalid_data', 'code-change events must be a list')
                for event in value['events']:
                    if not isinstance(event, dict):
                        raise DataError('invalid_data', 'code-change event must be an object')
                    if event.get('factor_basis_continuity_verified') is not True:
                        changes.append(event)
            change_status = 'checked' if datasets and checked else 'unknown'
            daily = self._daily_fact(daily_tables)
            errors = []
            if not daily_tables:
                errors.append('日线行情表未登记')
            for name, count in daily['missing_fields'].items():
                if count:
                    errors.append(f'日线字段 {name} 缺失 {count} 行')
            for key, label in (('duplicate_keys', '日线重复键'), ('invalid_price_rows', '日线价格异常')):
                if daily[key]:
                    errors.append(f'{label} {daily[key]} 行')
            if daily['price_basis_issues']:
                errors.append(f"成交额与价格口径异常 {len(daily['price_basis_issues'])} 个代码")
            for kind, label in (('stock_info', '股票主档'), ('valuation', '估值因子'), ('fundamentals', '基本面因子')):
                if facts[kind]['missing']:
                    errors.append(f'{label}事实表未登记')
            for key, count in daily['trade_state_issues'].items():
                if count:
                    errors.append(f'交易状态 {key} {count} 行')
            if change_status != 'checked':
                errors.append(f'代码变更事实{change_status}，未完成检查')
            if invalid:
                errors.append(f'标准事实无效 {invalid} 个')
            if len(coverage) < counts['files']:
                errors.append(f"标准事实仅抽样检查 {len(coverage)}/{counts['files']} 个")
            return {'daily': daily, **facts, 'errors': errors,
                    'partition_sha256': {reference: self._fingerprint(reference) for reference in daily_tables},
                    'saved_factor_files': counts['factors'],
                    'facts': {'files': counts['files'], 'checked': len(coverage), 'valid': valid,
                              'invalid': invalid, 'check_scope': 'all' if len(coverage) == counts['files'] else 'sample'},
                    'unverified_code_changes': changes, 'code_change_status': change_status}

    def search(self, query):
        if not isinstance(query, str):
            raise TypeError('q must be text')
        with self.data.transaction(readonly=True):
            tables = self._tables('stock_info')
            if not tables:
                raise DataError('missing', 'canonical stock_info table is required')
            stocks = self._source(tables)
            columns = self._columns(stocks)
            fields = ', '.join(f'{self._column(name, columns)} AS {self._name(name)}'
                               for name in ('code', 'name', 'exchange', 'status', 'first_date', 'last_date'))
            sql = f"""WITH stocks AS (SELECT {fields} FROM {stocks}
                WHERE cast(code AS VARCHAR) LIKE '%' || ? || '%'
                   OR coalesce(cast(name AS VARCHAR), '') LIKE '%' || ? || '%'
                ORDER BY code LIMIT 20)"""
            daily_tables = self._tables('daily')
            if daily_tables:
                daily = self._source(daily_tables)
                columns = self._columns(daily)
                change = self._column('pctChg', columns)
                sql += f""", latest AS (SELECT b.code, b.date, b.close, b.amount, {change} AS pctChg,
                    row_number() OVER (PARTITION BY b.code ORDER BY cast(b.date AS DATE) DESC) AS position
                    FROM {daily} b INNER JOIN stocks s ON cast(b.code AS VARCHAR)=cast(s.code AS VARCHAR))
                    SELECT s.*, l.date AS market_date, l.close, l.pctChg, l.amount
                    FROM stocks s LEFT JOIN latest l ON cast(l.code AS VARCHAR)=cast(s.code AS VARCHAR)
                    AND l.position=1 ORDER BY s.code LIMIT 20"""
            else:
                sql += " SELECT *, NULL AS market_date, NULL AS close, NULL AS pctChg, NULL AS amount FROM stocks ORDER BY code LIMIT 20"
            rows = self._rows(sql, (query, query))
            items = []
            for row in rows:
                status = row['status']
                if status in ('listed', '现存', True):
                    status = '现存'
                elif status is not None:
                    status = '退市'
                items.append({'代码': row['code'], '名称': row['name'], '交易所': row['exchange'],
                              '状态': status, '首日': row['first_date'], '末日': row['last_date'],
                              '最新收盘': row['close'], '涨跌幅': row['pctChg'],
                              '成交额亿': None if row['amount'] is None else float(row['amount']) / 100000000,
                              '行情日期': None if row['market_date'] is None else str(row['market_date'])})
            return {'items': items}

    def profile_report(self):
        facts = self.source_facts()
        daily = facts["daily"]
        first, last = daily.get("first_date"), daily.get("last_date")
        interval = "" if first is None or last is None else f"{first} ~ {last}"
        overview = {
            "日线行情": {"行数": daily["rows"], "股票数": daily["symbols"],
                         "区间": interval, "落后交易日": None},
            "股票主档": {"行数": facts["stock_info"]["rows"],
                         "现存": facts["stock_info"].get("listed"),
                         "退市": facts["stock_info"].get("delisted_or_unknown")},
            "估值因子": {"行数": facts["valuation"]["rows"], "股票数": facts["valuation"]["symbols"],
                         "区间": f"{facts['valuation'].get('first_date')} ~ {facts['valuation'].get('last_date')}"},
            "基本面因子": {"行数": facts["fundamentals"]["rows"], "股票数": facts["fundamentals"]["symbols"],
                           "区间": f"{facts['fundamentals'].get('first_date')} ~ {facts['fundamentals'].get('last_date')}"}
        }
        fields = {field: {"有值率": daily["field_coverage"].get(field),
                          "覆盖股数": daily["field_symbols"].get(field)}
                  for field in EXECUTION_FIELDS}
        complete = not facts["errors"] and daily["rows"] > 0
        return {"生成时间": datetime.now().isoformat(), "总览": overview,
                "年度覆盖": daily["yearly"], "日线字段": fields,
                "估值因子字段": facts["valuation"]["field_stats"],
                "基本面因子字段": facts["fundamentals"]["field_stats"],
                "异常检查": facts["errors"],
                "回测区间": {"区间": interval, "行数": daily["rows"],
                             "股票数": daily["symbols"], "完整": complete},
                "结论": {"级别": "✅" if complete else "⚠️",
                         "要点": facts["errors"], "回测可用": complete}}

    def research_report(self):
        facts = self.source_facts()
        daily = facts["daily"]
        return {"summary": {"first_date": daily.get("first_date") or "",
                             "last_date": daily.get("last_date") or "",
                             "rows": daily["rows"], "symbols": daily["symbols"],
                             "adjusted_close_rows": daily["adjusted_close_rows"],
                             "invalid_price_rows": daily["invalid_price_rows"]},
                "duplicate_keys": daily["duplicate_keys"],
                "price_basis_issues": daily["price_basis_issues"],
                "trade_state_issues": daily["trade_state_issues"],
                "freshness": {"latest_date": daily.get("latest_date"),
                              "latest_stock_count": daily["latest_stock_count"],
                              "tracked_stock_count": daily["tracked_stock_count"],
                              "median_stock_date": daily.get("median_stock_date")},
                "unverified_code_changes": (facts["unverified_code_changes"]
                                             if facts["code_change_status"] == "checked"
                                             else [{"status": facts["code_change_status"]}]),
                "saved_factor_files": facts["saved_factor_files"],
                "fact_check": facts["facts"],
                "partition_sha256": facts["partition_sha256"], "ranking_status": "NOT_RELEASED",
                "requirements_before_ranking": list(facts["errors"])}

    def doctor_report(self):
        facts = self.source_facts()
        registry = self.data.registry()
        rows = registry.get("sources", []) if isinstance(registry, Mapping) else []
        def repair_source(data_type):
            matches = [row for row in rows if isinstance(row, Mapping)
                       and row.get("data_type") == data_type
                       and row.get("default") is True
                       and set(row.get("modes", ())) & {"fix", "doctor"}]
            return dict(matches[0]) if len(matches) == 1 else None
        items = []
        for index, issue in enumerate(facts["errors"], 1):
            data_type = None
            if issue.startswith("日线") or issue.startswith("成交额"):
                data_type = "daily"
            elif issue.startswith("股票主档"):
                data_type = "stock_info"
            elif issue.startswith("估值因子"):
                data_type = "valuation"
            elif issue.startswith("基本面因子"):
                data_type = "fundamentals"
            source = repair_source(data_type) if data_type else None
            fix = None if source is None else {
                "source_id": source["id"], "mode": "fix",
                "data_type": data_type, "force": False,
            }
            items.append({"id": f"fact:{index}", "级别": "error", "问题": issue,
                          "可修复": fix is not None, "fix": fix,
                          "动作": "按登记源补齐" if fix else "无唯一登记源",
                          "范围": data_type or "source",
                          "说明": "由登记源执行同一 Collect 流程" if fix
                          else "当前没有可安全选择的登记源，不能猜测修复"})
        return {"items": items}

    def query(self, value):
        from protocol import DataQuery
        dataset = value["dataset_id"]
        fields = tuple(value.get("fields", ("open", "high", "low", "close", "volume", "amount")))
        symbols = value.get("symbols")
        query = DataQuery(dataset, date.fromisoformat(value["start"]),
                          date.fromisoformat(value["end"]), fields,
                          int(value.get("history", 1)), value.get("price_basis", "raw"),
                          symbols=None if symbols is None else tuple(symbols),
                          universe=value.get("universe", "dataset"))
        result = self.data.query(query)
        rows = []
        for record in result.records:
            for symbol, values in record.frame.fields.items():
                rows.append({"code": symbol, "date": record.trade_date.isoformat(), **values})
        return {"items": rows}


    def bars(self, value):
        frequencies = {"分时", "日", "周", "月", "年"}
        adjustments = {"不复权", "前复权", "后复权"}
        freq, adj = value.get("freq", "日"), value.get("adj", "不复权")
        if freq not in frequencies or adj not in adjustments:
            raise ValueError("非法参数")
        if freq == "分时":
            source = self.sources.select(mode="kline", data_type="minute")
            if source.get("frequency") not in {"minute", "intraday"}:
                raise ValueError("分时源频率无效")
            raise ValueError("分时源尚未提供标准查询字段")
        source = self.sources.select(mode="kline", data_type="daily")
        dataset = source.get("dataset_id")
        calendar_name = source.get("calendar")
        if not isinstance(dataset, str) or not isinstance(calendar_name, str):
            raise ValueError("daily kline source must declare dataset_id and calendar")
        calendar = self.data.calendar(dataset)
        sessions = [date.fromisoformat(str(item)) for item in calendar.get("sessions", ())]
        if len(sessions) < 2:
            raise ValueError("daily kline calendar is missing")
        value = {**value, "dataset_id": dataset, "symbols": [value["code"]],
                 "start": value.get("start", sessions[0].isoformat()),
                 "end": value.get("end", sessions[-2].isoformat())}
        value["price_basis"] = "raw" if adj == "不复权" else "adjusted"
        result = self.query(value)
        rows = result["items"]
        rows.sort(key=lambda row: row["date"])
        if freq != "日":
            groups = {}
            for row in rows:
                day = date.fromisoformat(row["date"])
                if freq == "周":
                    key = day.isocalendar()[:2]
                elif freq == "月":
                    key = (day.year, day.month)
                else:
                    key = (day.year,)
                groups.setdefault(key, []).append(row)
            rows = []
            for values in groups.values():
                values.sort(key=lambda item: item["date"])
                first, last = values[0], values[-1]
                rows.append({"date": last["date"],
                             "open": first.get("open"),
                             "high": max(item["high"] for item in values),
                             "low": min(item["low"] for item in values),
                             "close": last.get("close"),
                             "volume": sum(item.get("volume") or 0 for item in values),
                             "amount": sum(item.get("amount") or 0 for item in values)})
        return {
            "dates": [row["date"] for row in rows],
            "candles": [[row.get("open"), row.get("close"), row.get("low"), row.get("high")]
                        for row in rows],
            "volumes": [row.get("volume") for row in rows],
        }
