"""Move existing code-change events into the registered source table."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

from market import build_data, table_name
from protocol import DataError, TableDef

KEYS = ('exchange', 'effective_date', 'old_code', 'new_code')
SOURCES = ('stock_security', 'security_changes', 'stock_adjust', 'stock_dividend')
TYPES = {'exchange': 'VARCHAR', 'effective_date': 'VARCHAR', 'old_code': 'VARCHAR',
         'new_code': 'VARCHAR', 'announcement_date': 'VARCHAR', 'source_url': 'VARCHAR',
         'evidence_sha256': 'VARCHAR', 'holder_share_ratio': 'BIGINT',
         'holding_period_continues': 'BOOLEAN', 'factor_basis_continuity_verified': 'BOOLEAN',
         'factor_basis_evidence': 'JSON'}


def _event(row):
    value = {key: item for key, item in row.items() if item is not None}
    if isinstance(value.get('factor_basis_evidence'), str):
        value['factor_basis_evidence'] = json.loads(value['factor_basis_evidence'])
    if any(not value.get(key) for key in KEYS):
        raise ValueError('code-change event is missing its identity')
    return value


def migrate(data, dataset, source):
    configured = json.loads((Path(__file__).resolve().parents[1] /
                             'data/source/registry/sources.json').read_text())
    descriptors = {item['id']: item for item in configured['sources'] if item['id'] in SOURCES}
    if set(descriptors) != set(SOURCES):
        raise ValueError('required sources are missing from registry')
    desired = {item['scope']: item for item in configured['markets']
               if item.get('scope') in ('CN_A', 'CN_A_SH_SZ')}
    if set(desired) != {'CN_A', 'CN_A_SH_SZ'}:
        raise ValueError('required markets are missing from registry')
    with data.transaction():
        def register(registry):
            for identity in SOURCES:
                matches = [item for item in registry['sources'] if item['id'] == identity]
                if len(matches) > 1 or (matches and matches[0] != descriptors[identity]):
                    raise ValueError('conflicting source: ' + identity)
                if not matches:
                    registry['sources'].append(deepcopy(descriptors[identity]))
            for scope, expected in desired.items():
                matches = [item for item in registry['markets'] if item.get('scope') == scope]
                if len(matches) != 1:
                    raise ValueError('market is missing or duplicated: ' + scope)
                collect = matches[0]['collect']
                for identity in SOURCES:
                    binding = expected['collect']['data'][identity]
                    if identity in collect['data'] and collect['data'][identity] != binding:
                        raise ValueError('conflicting market binding: ' + identity)
                    collect['data'][identity] = deepcopy(binding)
                    if identity not in collect['default']:
                        collect['default'].append(identity)
            return registry
        data.update_registry(register)
        manifest = data.manifest(dataset)
        reference = manifest['inputs']['code_changes']
        old = (data.table(reference).to_pylist() if reference.startswith('lake:')
               else data.object(reference)['events'])
        try:
            bound = data.source(source)
        except DataError as error:
            if error.code != 'missing':
                raise
            bound = None
        existing = data.table(bound).to_pylist() if bound else []
        merged = {}
        for row in [*existing, *old]:
            row = _event(row)
            identity = tuple(row[key] for key in KEYS)
            prior = merged.setdefault(identity, {})
            for key, value in row.items():
                if key in prior and prior[key] != value:
                    raise ValueError('conflicting code-change event: ' + repr(identity))
                prior[key] = value
        old_codes, new_codes, outgoing = set(), set(), {}
        for event in merged.values():
            old, new = event['old_code'], event['new_code']
            if old == new or old in old_codes or new in new_codes:
                raise ValueError('conflicting code-change graph')
            old_codes.add(old)
            new_codes.add(new)
            outgoing[old] = event
        for start in outgoing:
            code, previous, visited = start, None, set()
            while code in outgoing:
                if code in visited:
                    raise ValueError('cyclic code-change graph')
                visited.add(code)
                event = outgoing[code]
                if previous is not None and event['effective_date'] <= previous:
                    raise ValueError('code-change dates are not increasing')
                previous = event['effective_date']
                code = event['new_code']
        name = bound[5:] if bound else table_name(source + '/records')
        rows = list(merged.values())
        if bound:
            definition = data.definition(name)
            if definition.keys != KEYS:
                raise ValueError('code-change source has different identity keys')
            types = dict(definition.columns)
        else:
            types = {key: TYPES[key] for key in TYPES if key in set(KEYS) or any(key in row for row in rows)}
            data.define(TableDef(name, types, KEYS, tuple(key for key in types if key not in KEYS)))
        for row in rows:
            for key, value in row.items():
                if key not in types:
                    if key in TYPES:
                        types[key] = TYPES[key]
                    elif isinstance(value, (dict, list)):
                        types[key] = 'JSON'
                    elif isinstance(value, bool):
                        types[key] = 'BOOLEAN'
                    elif isinstance(value, int):
                        types[key] = 'BIGINT'
                    elif isinstance(value, float):
                        types[key] = 'DOUBLE'
                    elif isinstance(value, str):
                        types[key] = 'VARCHAR'
                    else:
                        raise ValueError('unknown code-change field: ' + key)
        if rows:
            data.put_rows(name, rows, keys=KEYS, types=types, refresh=True)
            actual = {tuple(event[key] for key in KEYS): event
                      for row in data.table('lake:' + name).to_pylist() if (event := _event(row))}
            if actual != merged:
                raise ValueError('code-change source did not preserve all event fields')
        if not bound:
            data.bind(source, 'records', 'lake:' + name)
        manifest = deepcopy(manifest)
        manifest['inputs']['code_changes'] = 'changes:' + name
        data.save_manifest(dataset, manifest)
        return len(rows)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: python tools/migrate_changes.py <project-root>')
    data = build_data(Path(sys.argv[1]))
    try:
        print(migrate(data, 'cn_a', 'security_changes'))
    finally:
        data.close()
