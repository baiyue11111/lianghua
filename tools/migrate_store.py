"""Offline migration from the DuckLake catalog to the Parquet + DuckDB layout.

The catalog format changed: bulk tables are now immutable Parquet files listed in
``catalog.duckdb`` instead of DuckLake-managed files. This copies every logical
table from an old ``data/catalog.ducklake`` into a new project directory, decides
the layer per table (state stays in the database file, bulk becomes Parquet) and
verifies each table with the same canonical fingerprint the runtime uses.

    python tools/migrate_store.py --source . --target /tmp/lianghua-store
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from protocol import TableDef, TableQuery, TableWrite  # noqa: E402
from storage import DataStore, canonical  # noqa: E402

BATCH = 65536
BULK = ('fact_adjust', 'fact_dividend', 'fact_security',
        'run_equity', 'run_events', 'run_trades', 'run_positions', 'run_trials')


def layer(name):
    """Bulk tables are append-only analysis output; everything else is state.

    An imported market table is named ``source_<sha256>``; ``source_tables`` is
    the binding registry and stays in the database file with the other state.
    """
    imported = re.fullmatch(r'source_[0-9a-f]{24}', name) is not None
    return 'parquet' if imported or name in BULK else 'table'


def fingerprint(connection, name, definition):
    columns = list(definition['columns'])
    projection = ', '.join('"' + column.replace('"', '""') + '"' for column, _ in columns)
    fields = ', '.join('"' + column.replace('"', '""') + '" := row."'
                       + column.replace('"', '""') + '"' for column, _ in columns)
    keys = ', '.join('row."' + key.replace('"', '""') + '"' for key in definition['keys'])
    schema = canonical(columns)
    sql = ("SELECT sha256(? || '\n' || coalesce("
           f"string_agg(to_json(struct_pack({fields})), '\n' ORDER BY {keys}), '')) "
           f'FROM (SELECT {projection} FROM lake.main."{name}") AS row')
    return connection.execute(sql, [schema]).fetchone()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='.')
    parser.add_argument('--target', required=True)
    options = parser.parse_args()
    source = Path(options.source).resolve()
    target = Path(options.target).resolve()

    reader = duckdb.connect(':memory:')
    reader.execute("LOAD ducklake")
    reader.execute("SET ducklake_default_data_inlining_row_limit = 0")
    reader.execute(f"ATTACH 'ducklake:{source / 'data/catalog.ducklake'}' AS lake"
                   f" (DATA_PATH '{source / 'data/parquet/'}/', OVERRIDE_DATA_PATH true,"
                   ' DATA_INLINING_ROW_LIMIT 0)')
    reader.execute("CALL lake.set_option('data_inlining_row_limit', 0)")
    tables = [(name, json.loads(text)) for name, text in
              reader.execute('SELECT name, definition FROM lake.main._tables ORDER BY name').fetchall()]

    store = DataStore(target)
    report = []
    for name, definition in tables:
        definition.setdefault('storage', layer(name))
        definition['storage'] = layer(name)
        table = TableDef(**{**definition, 'columns': dict(definition['columns'])})
        store.define(table)
        started = time.perf_counter()
        count = 0
        batches = reader.execute(f'SELECT * FROM lake.main."{name}"').to_arrow_reader(BATCH)
        for batch in batches:
            store.write(TableWrite(name, batch))
            count += batch.num_rows
        elapsed = time.perf_counter() - started
        stored = store.scan(TableQuery(name)).table.num_rows
        before = fingerprint(reader, name, definition)
        after = store.fingerprint(name)
        report.append((name, table.storage, count, stored, before == after, elapsed))
        print(f'{name:34s} {table.storage:7s} rows={count:8d} stored={stored:8d} '
              f'fingerprint={"match" if before == after else "DIFFERENT"} {elapsed:6.2f}s')

    store.close()
    reader.close()
    broken = [row for row in report if row[2] != row[3] or not row[4]]
    print(f'\n{len(report)} tables, {sum(row[2] for row in report)} rows, '
          f'{len(broken)} mismatched')
    return 1 if broken else 0


if __name__ == '__main__':
    raise SystemExit(main())
