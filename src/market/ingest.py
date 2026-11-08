"""Explicit imports into the standard relational data port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from pathlib import Path
from io import BytesIO
from typing import Any
from zipfile import ZipFile

import pyarrow as pa

from .xlsx_matrix import iter_sheet_rows


def import_legacy(data, project: Path, *, datasets=None, symbols=None, start=None, end=None):
    """Explicit offline import. Runtime readers never inspect this source tree."""
    from copy import deepcopy
    from market import table_name, fact_ref
    from protocol import DataError, normalize_security_id
    import duckdb

    root = Path(project).resolve() / 'data' / 'source'
    registry = json.loads((root / 'registry' / 'sources.json').read_text())
    selected = set(datasets) if datasets is not None else {item['id'] for item in registry['datasets']}
    requested = None if symbols is None else {normalize_security_id(code) for code in symbols}
    converted = {}
    counts = {}
    connection = duckdb.connect(config={'threads': '4'})

    def source_path(dataset, name):
        candidate = (root.parent / name) if name.startswith('source/') else (root / dataset / name)
        candidate = candidate.resolve()
        if not candidate.is_relative_to(root):
            raise DataError('invalid_path', 'import reference escapes the explicit source root')
        return candidate

    def import_table(identity, kind, paths, keys, *, price=False):
        name = table_name(f'{identity}/{kind}')
        relation = connection.read_parquet([str(path) for path in paths], union_by_name=True)
        if price and requested is not None:
            aliases = sorted(requested | {code.split('.')[-1] for code in requested})
            connection.register('_codes', pa.table({'code': aliases}))
            relation = relation.filter('code IN (SELECT code FROM _codes)')
        if price and start is not None:
            relation = relation.filter("CAST(date AS DATE) >= DATE '" + date.fromisoformat(str(start)).isoformat() + "'")
        if price and end is not None:
            relation = relation.filter("CAST(date AS DATE) <= DATE '" + date.fromisoformat(str(end)).isoformat() + "'")
        count = 0
        for batch in relation.to_arrow_reader(65536):
            table = pa.Table.from_batches([batch])
            if keys is None:
                table = table.append_column('_row_id', pa.array(range(count, count + table.num_rows), type=pa.int64()))
            data.put_rows(name, table, keys=keys or ('_row_id',))
            count += table.num_rows
        if not count:
            raise DataError('missing', f'import table is empty: {identity}/{kind}')
        reference = 'lake:' + name
        data.bind(identity, kind, reference)
        counts[f'{identity}/{kind}'] = count
        return reference

    def convert(dataset, name):
        path = source_path(dataset, name)
        if path in converted:
            return converted[path]
        if path.suffix == '.parquet':
            # Each numeric field keeps its declared code/effective/known binding.
            manifest = originals[dataset]
            bindings = [spec for spec in manifest.get('fields', {}).values()
                        if isinstance(spec, dict) and spec.get('file') == name]
            if not bindings:
                raise DataError('invalid_schema', 'numeric table requires explicit field bindings: ' + name)
            reference = import_table(str(path.relative_to(root)), 'facts', [path], None)
        elif path.suffix == '.json':
            value = json.loads(path.read_text())
            if isinstance(value, dict) and value.get('schema') == 1 and value.get('kind') in ('adjust', 'security', 'dividend', 'no_trade'):
                reference = data.save_fact(value)
            elif path.name == 'calendar.json':
                data.save_calendar(dataset, value)
                reference = 'calendar:' + dataset
            else:
                value = deepcopy(value)
                if path.name == 'account_policy.json':
                    for source in value.get('sources', []):
                        old_name = source['file']
                        if not old_name.startswith('source/'):
                            old_name = 'source/' + str((path.parent / old_name).relative_to(root))
                        source['file'] = attachment(source_path(dataset, old_name))
                reference = data.save_config(str(path.relative_to(root)), value)
        else:
            reference = attachment(path)
        converted[path] = reference
        return reference

    def attachment(path):
        content = path.read_bytes()
        identity = str(path.relative_to(root))
        data.put_rows('attachments', [{'id': identity, 'content_type': 'application/octet-stream',
                                      'sha256': sha256(content).hexdigest(), 'content': content}], keys=('id',))
        reference = 'attachment:' + identity
        converted[path] = reference
        return reference

    originals = {identity: json.loads((root / identity / 'manifest.json').read_text()) for identity in selected}
    for section in ('markets', 'sources'):
        for item in registry.get(section, []):
            if item.get('dataset_id') in selected and 'calendar' in item:
                item['calendar'] = 'calendar:' + item['dataset_id']
    try:
        with data.transaction():
            data.update_registry(lambda _: registry)
            for identity, original in originals.items():
                manifest = deepcopy(original)
                old_inputs = original.get('inputs', {})
                bars_folder = source_path(identity, old_inputs.get('bars', 'daily'))
                paths = sorted(bars_folder.glob('*.parquet'))
                if not paths:
                    raise DataError('missing', 'dataset has no source prices: ' + identity)
                bars = import_table(identity, 'bars', paths, ('code', 'date'), price=True)
                for path in paths:
                    converted[path.resolve()] = bars
                manifest['calendar'] = convert(identity, original.get('calendar', old_inputs.get('calendar', 'calendar.json')))
                inputs = {'bars': bars, 'calendar': manifest['calendar']}
                if old_inputs:
                    for key in ('account_policy', 'code_changes'):
                        inputs[key] = convert(identity, old_inputs[key])
                    for key, kind in (('adjustments', 'adjust'), ('securities', 'security'), ('distributions', 'dividend')):
                        spec = deepcopy(old_inputs[key])
                        template = spec['template']
                        wildcard = re.sub(r'\{[^}]+\}', '*', template)
                        patterns = [wildcard]
                        if requested is not None:
                            patterns = [template.format(symbol=code, start=spec.get('start'), end=spec.get('end'), year='*')
                                        for code in sorted(requested)]
                        paths = sorted({path for pattern in patterns for path in (
                            root.glob(pattern[7:]) if pattern.startswith('source/') else (root / identity).glob(pattern))})
                        for path in paths:
                            payload = json.loads(path.read_text())
                            if requested is not None and normalize_security_id(payload['code']) not in requested:
                                continue
                            convert(identity, 'source/' + str(path.relative_to(root)))
                        spec['template'] = fact_ref(kind, '{symbol}', spec.get('start'), spec.get('end'), '{year}' if kind == 'dividend' else None)
                        spec['template'] = spec['template'].replace('%7Bsymbol%7D', '{symbol}').replace('%7Byear%7D', '{year}')
                        inputs[key] = spec
                    inputs['action_facts'] = deepcopy(old_inputs.get('action_facts', []))
                    for action in inputs['action_facts']:
                        for key in ('source_file', 'credit_source_file', 'tax_source_file'):
                            action[key] = convert(identity, action[key])
                manifest['inputs'] = inputs
                for spec in manifest.get('fields', {}).values():
                    if 'file' in spec:
                        spec['file'] = convert(identity, spec['file'])
                refs = []
                seen = set()
                for ref in original.get('files', []):
                    reference = convert(identity, ref['file'])
                    if reference not in seen:
                        refs.append({'file': reference, 'role': ref.get('role', 'market')})
                        seen.add(reference)
                manifest['files'] = refs
                manifest['schema'] = 3
                if requested is not None:
                    manifest['scope']['symbols'] = sorted(code for code in manifest['scope']['symbols']
                                                          if normalize_security_id(code) in requested)
                data.save_manifest(identity, manifest)
        return counts
    finally:
        connection.close()


PRICE_FIELDS = ("open", "close", "high", "low", "volume", "amount")
PRICE_SHEETS = dict(zip(PRICE_FIELDS, ("开盘价", "收盘价", "最高价", "最低价", "成交量", "成交额"), strict=True))
FUNDAMENTAL_SHEETS = {
    "net_income_ttm": "归母净利润(TTM)",
    "net_assets": "净资产",
    "roe": "ROE",
}
VALUATION_SHEETS = {"pe": "市盈率(TTM)", "pb": "市净率"}


@dataclass(frozen=True)
class IngestionResult:
    dataset_id: str
    source_hashes: dict[str, str]
    rows: dict[str, int]
    limitations: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_exploratory(*, price_workbook: Path, fundamental_workbook: Path,
                       valuation_workbook: Path, data, dataset: str,
                       max_symbols: int | None = None,
                       only_price_parts: tuple[int, ...] | None = None) -> IngestionResult:
    """Stream workbook cells into the same managed source tables."""
    from market import table_name
    if max_symbols is not None and (type(max_symbols) is not int or max_symbols < 1):
        raise ValueError('max_symbols must be a positive integer')
    symbols, calendar, row_count = [], None, 0
    bars = table_name(dataset + '/bars')
    with data.transaction():
        for workbook in _price_books(price_workbook, only_price_parts):
            streams = {field: iter_sheet_rows(workbook, sheet) for field, sheet in PRICE_SHEETS.items()}
            headers = {field: next(stream) for field, stream in streams.items()}
            header = headers['close']
            if any(value != header for value in headers.values()):
                raise ValueError('price sheets must share an identical calendar')
            days = [date.fromisoformat(str(value)) for value in header[2:] if value is not None]
            if not days or days != sorted(set(days)):
                raise ValueError('price calendar must be ordered and unique')
            if calendar is None:
                calendar = days
            elif calendar != days:
                raise ValueError('price archive parts must share an identical calendar')
            batch = []
            for combined in zip(*(streams[field] for field in PRICE_FIELDS), strict=True):
                if max_symbols is not None and len(symbols) >= max_symbols:
                    break
                codes = {str(row[0]).zfill(6) for row in combined}
                names = {str(row[1]) for row in combined}
                if len(codes) != 1 or len(names) != 1:
                    raise ValueError('price sheet rows are not aligned by symbol and name')
                code, name = codes.pop(), names.pop()
                if code in symbols:
                    raise ValueError('duplicate security across price workbook parts')
                symbols.append(code)
                for index, day in enumerate(days, 2):
                    batch.append({'code': code, 'name': name, 'date': day.isoformat(),
                                  **{field: _as_float(row[index]) if index < len(row) else None
                                     for field, row in zip(PRICE_FIELDS, combined, strict=True)}})
                if len(batch) >= 65536:
                    data.put_rows(bars, batch, keys=('code', 'date'),
                                  types={field: 'DOUBLE' for field in PRICE_FIELDS})
                    row_count += len(batch)
                    batch = []
            if batch:
                data.put_rows(bars, batch, keys=('code', 'date'),
                              types={field: 'DOUBLE' for field in PRICE_FIELDS})
                row_count += len(batch)
        if not symbols:
            raise ValueError('price workbook contains no price rows')
        valuation_source = (_archive_member(valuation_workbook, 'raw_fundamentals_matrix_full.xlsx')
                            if valuation_workbook.suffix.lower() == '.zip' else valuation_workbook)
        panels = _fundamental_rows(fundamental_workbook, valuation_source)
        facts = []
        for panel in panels:
            for index, period in enumerate(panel['period']):
                facts.append({'code': panel['symbol'], 'name': panel['name'], 'quarter': period,
                              'industry': panel['industry'],
                              **{field: panel[field][index] for field in (*FUNDAMENTAL_SHEETS, *VALUATION_SHEETS)}})
        fact_table = table_name(dataset + '/fundamentals')
        data.put_rows(fact_table, facts, keys=('code', 'quarter'),
                      types={field: 'DOUBLE' for field in (*FUNDAMENTAL_SHEETS, *VALUATION_SHEETS)})
        data.bind(dataset, 'bars', 'lake:' + bars)
        data.bind(dataset, 'fundamentals', 'lake:' + fact_table)
        data.save_calendar(dataset, {'sessions': [day.isoformat() for day in calendar],
                                     'coverage': [{'start': calendar[0].isoformat(), 'end': calendar[-1].isoformat()}]})
        data.save_manifest(dataset, {'schema': 3, 'calendar': 'calendar:' + dataset,
                                    'scope': {'symbols': sorted(symbols), 'universe': 'fixed_symbols'},
                                    'fields': {}, 'files': [{'file': 'lake:' + bars, 'role': 'market'}],
                                    'inputs': {'bars': 'lake:' + bars, 'calendar': 'calendar:' + dataset}})
    return IngestionResult(dataset_id=dataset,
                           source_hashes={key: sha256_file(path) for key, path in (
                               ('price_workbook', price_workbook), ('fundamental_workbook', fundamental_workbook),
                               ('valuation_workbook', valuation_workbook))},
                           rows={'prices': row_count, 'fundamentals': len(facts), 'calendar': len(calendar)},
                           limitations=('SURVIVORSHIP_BIAS_CURRENT_UNIVERSE', 'STATIC_INDUSTRY_MAPPING',
                                        'FUNDAMENTAL_PUBLICATION_TIME_UNAVAILABLE', 'CORPORATE_ACTION_FACTS_UNAVAILABLE'))


def _price_books(path, selected):
    if path.suffix.lower() != '.zip':
        if selected is not None:
            raise ValueError('price_part requires a price archive')
        yield path
        return
    with ZipFile(path) as archive:
        members = sorted(name for name in archive.namelist()
                         if name.startswith('raw_prices_matrix_full_part') and name.endswith('.xlsx'))
        if not members:
            raise ValueError('price archive has no price workbook parts')
        if selected is not None and (not selected or any(type(index) is not int or index < 0 or index >= len(members)
                                                        for index in selected)):
            raise ValueError('price_part contains an invalid part index')
        for index, name in enumerate(members):
            if selected is None or index in selected:
                yield BytesIO(archive.read(name))


def _archive_member(archive_path: Path, member_name: str) -> BytesIO:
    with ZipFile(archive_path) as archive:
        return BytesIO(archive.read(member_name))


def _fundamental_rows(fundamental_workbook: Path, valuation_workbook: Path) -> list[dict[str, Any]]:
    streams = {
        field: iter_sheet_rows(fundamental_workbook, sheet) for field, sheet in FUNDAMENTAL_SHEETS.items()
    } | {field: iter_sheet_rows(valuation_workbook, sheet) for field, sheet in VALUATION_SHEETS.items()}
    headers = {field: next(stream) for field, stream in streams.items()}
    source_periods = {
        field: {str(period): index for index, period in enumerate(header) if index >= 2 and period is not None}
        for field, header in headers.items()
    }
    periods = [
        str(period)
        for period in headers["roe"][2:]
        if period is not None and all(str(period) in period_map for period_map in source_periods.values())
    ]
    if not periods:
        raise ValueError("fundamental and valuation sheets have no common periods")
    industries = _industries(fundamental_workbook)
    values_by_field: dict[str, dict[str, tuple[str, list[float | None]]]] = {}
    for field, stream in streams.items():
        rows_for_field: dict[str, tuple[str, list[float | None]]] = {}
        for row in stream:
            if len(row) < 2 or row[0] is None:
                continue
            symbol = str(row[0]).zfill(6)
            rows_for_field[symbol] = (
                str(row[1]),
                [
                    _as_float(row[source_periods[field][period]])
                    if len(row) > source_periods[field][period]
                    else None
                    for period in periods
                ],
            )
        values_by_field[field] = rows_for_field
    symbols = sorted(set().union(*(values.keys() for values in values_by_field.values())))
    return [
        {
            "symbol": symbol,
            "name": next(values[symbol][0] for values in values_by_field.values() if symbol in values),
            "period": periods,
            "industry": industries.get(symbol),
            **{
                field: values_by_field[field].get(symbol, ("", [None] * len(periods)))[1]
                for field in values_by_field
            },
        }
        for symbol in symbols
    ]


def _industries(workbook: Path) -> dict[str, str]:
    rows = iter_sheet_rows(workbook, "股票信息")
    header = next(rows)
    try:
        code_index = header.index("代码")
        industry_index = header.index("申万一级")
    except ValueError as error:
        raise ValueError("fundamental workbook lacks 代码 or 申万一级") from error
    return {
        str(row[code_index]).zfill(6): str(row[industry_index])
        for row in rows
        if len(row) > max(code_index, industry_index) and row[code_index] is not None and row[industry_index] is not None
    }


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
