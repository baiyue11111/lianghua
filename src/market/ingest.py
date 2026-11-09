"""Explicit imports into the standard relational data port."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from hashlib import sha256
import json
import os
import re
from pathlib import Path

from pyarrow import parquet as pq


# The source archive spells the same instrument two ways: 2003-2012 files carry a
# market prefix ("sh.600036") and 2013+ files carry the bare six-digit code. Price
# rows are stored under the identity normalize_security_id would produce, so one
# instrument is one key and a long history does not split across two of them.
CODE_IDENTITY = ("CASE"
                 " WHEN regexp_matches(lower(trim(code)), '^[a-z][a-z0-9_-]{0,7}\\.')"
                 " THEN lower(trim(code))"
                 " WHEN left(lower(trim(code)), 1) = '6' THEN 'sh.' || lower(trim(code))"
                 " WHEN regexp_matches(lower(trim(code)), '^(000|001|002|003|300|301|302)')"
                 " THEN 'sz.' || lower(trim(code))"
                 " ELSE lower(trim(code)) END")


def identifier(name):
    """A column name spelled as a quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def declared_pattern(template, spec):
    """A manifest fact template with only the varying parts left wild.

    ``start`` and ``end`` are fixed by the manifest, so they are filled in
    instead of globbed. Globbing them pulls in undeclared revisions of the same
    fact (a later adjust run), which can carry values the declared columns do
    not hold. Only symbol and year vary across the declared source files.
    """
    return re.sub(
        r'\{([^}]+)\}',
        lambda match: '*' if match.group(1) in ('symbol', 'year')
        else str(spec.get(match.group(1), '')),
        template)


def import_legacy(data, project: Path, *, datasets=None, symbols=None, start=None, end=None):
    """Explicit offline import. Runtime readers never inspect this source tree."""
    from copy import deepcopy
    from market import table_name, fact_ref
    from protocol import DataError, Filter, TableQuery, TableSelect, normalize_security_id

    root = Path(project).resolve() / 'data' / 'source'
    registry = json.loads((root / 'registry' / 'sources.json').read_text())
    selected = set(datasets) if datasets is not None else {item['id'] for item in registry['datasets']}
    requested = None if symbols is None else {normalize_security_id(code) for code in symbols}
    converted = {}
    counts = {}
    pending_facts = []

    def source_path(dataset, name):
        candidate = (root.parent / name) if name.startswith('source/') else (root / dataset / name)
        candidate = candidate.resolve()
        if not candidate.is_relative_to(root):
            raise DataError('invalid_path', 'import reference escapes the explicit source root')
        return candidate

    def import_table(identity, kind, paths, keys, *, price=False):
        name = table_name(f'{identity}/{kind}')
        listing = ', '.join("'" + str(path).replace("'", "''") + "'" for path in paths)
        sql = f'SELECT * FROM read_parquet([{listing}], union_by_name=true)'
        if price:
            sql = ('SELECT * EXCLUDE ("code"), ' + CODE_IDENTITY + ' AS code'
                   f' FROM ({sql}) AS priced')
        where, values = [], []
        if price and requested is not None:
            where.append('code IN (' + ', '.join(['?'] * len(requested)) + ')')
            values.extend(sorted(requested))
        if price and start is not None:
            where.append("CAST(date AS DATE) >= DATE '" + date.fromisoformat(str(start)).isoformat() + "'")
        if price and end is not None:
            where.append("CAST(date AS DATE) <= DATE '" + date.fromisoformat(str(end)).isoformat() + "'")
        if where:
            sql = f'SELECT * FROM ({sql}) AS scoped WHERE ' + ' AND '.join(where)
        if keys is None:
            # A surrogate key has to survive a re-import, so it follows the
            # content instead of whatever order the scan happened to produce.
            names = sorted({field for path in paths for field in pq.read_schema(path).names})
            order = ', '.join(identifier(field) for field in names)
            sql = f'SELECT *, row_number() OVER (ORDER BY {order}) - 1 AS _row_id FROM ({sql}) AS raw'
        result = data.put_rows(name, TableSelect(sql, tuple(values)),
                               keys=keys or ('_row_id',), storage='parquet')
        count = result.rows
        if not count:
            raise DataError('missing', f'import table is empty: {identity}/{kind}')
        if price and requested is not None:
            # A narrowed import replaces the dataset's price table, it does not
            # merge into it. Rows left over from a wider earlier import would
            # stay in the lake while the manifest says they are not there, so
            # the next reader inherits prices the scope denies.
            present = {row['code'] for row in
                       data.scan(TableQuery(name, columns=('code',))).table.to_pylist()}
            stale = sorted(present - requested)
            if stale:
                data.delete(TableQuery(name, (Filter('code', 'in', stale),)))
        reference = 'lake:' + name
        data.bind(identity, kind, reference)
        counts[f'{identity}/{kind}'] = count
        return reference

    def convert(dataset, name, payload=None):
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
            value = json.loads(path.read_text()) if payload is None else payload
            if isinstance(value, dict) and value.get('schema') == 1 and value.get('kind') in ('adjust', 'security', 'dividend', 'no_trade'):
                # Deferred: one grouped write replaces a transaction per payload.
                pending_facts.append(value)
                reference = fact_ref(value['kind'], value['code'],
                                     value['start'], value['end'], value['year'])
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

    @lru_cache(maxsize=None)
    def listing(folder):
        try:
            return os.listdir(root / folder)
        except (FileNotFoundError, NotADirectoryError):
            return ()

    def expand(identity, patterns):
        """Every source file a manifest pattern names, in one directory pass.

        A wildcard pattern globbed once per symbol reads the same 150k-entry
        directory once per symbol; the names are matched against all patterns
        together instead.
        """
        found, groups, globbed = [], {}, []
        for pattern in patterns:
            name = pattern[7:] if pattern.startswith('source/') else f'{identity}/{pattern}'
            folder, _, tail = name.rpartition('/')
            if not any(character in tail for character in '*?['):
                if (root / name).is_file():
                    found.append(root / name)
            elif not any(character in tail for character in '?[') and '**' not in tail:
                groups.setdefault(folder, []).append(
                    ''.join('[^/]*' if character == '*' else re.escape(character) for character in tail))
            else:
                globbed.append(name)
        for folder, bodies in groups.items():
            regex = re.compile('(?:' + '|'.join(bodies) + r')\Z')
            found.extend(root / folder / entry for entry in listing(folder) if regex.match(entry))
        for name in globbed:
            found.extend(root.glob(name))
        return found

    for section in ('markets', 'sources'):
        for item in registry.get(section, []):
            if item.get('dataset_id') in selected and 'calendar' in item:
                item['calendar'] = 'calendar:' + item['dataset_id']
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
                    wildcard = declared_pattern(template, spec)
                    patterns = [wildcard]
                    if requested is not None:
                        patterns = [template.format(symbol=code, start=spec.get('start'), end=spec.get('end'), year='*')
                                    for code in sorted(requested)]
                    paths = sorted(set(expand(identity, patterns)))
                    for path in paths:
                        payload = json.loads(path.read_text())
                        if requested is not None and normalize_security_id(payload['code']) not in requested:
                            continue
                        convert(identity, 'source/' + str(path.relative_to(root)), payload)
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
                declared = manifest['scope']['symbols']
                kept = sorted(code for code in declared if normalize_security_id(code) in requested)
                # The narrowed scope is what the data really holds; record the
                # shortfall so a later reader does not mistake it for the market.
                manifest['scope']['symbols'] = kept
                dropped = len(declared) - len(kept)
                if dropped:
                    limitations = list(manifest.get('limitations') or ())
                    limitations.append(
                        f'partial import: {len(kept)} of {len(declared)} declared symbols present')
                    manifest['limitations'] = limitations
            data.save_manifest(identity, manifest)
        data.save_facts(pending_facts)
        return counts
