"""Read-only all-partition price audit; no repair, source fallback or backtest.

Master membership is only the supplied catalogue, not a certification of the
historical A-share universe. Even a passing price gate does not authorize NAV
publication without corporate-action, tax, execution and evaluation evidence.
"""
import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import duckdb
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from adjusted_panel import AdjustmentCoverageError, load_adjusted_lake_panel


def _hash(path):
    with path.open('rb') as handle: return hashlib.file_digest(handle,'sha256').hexdigest()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('daily','cache','stock-basic','master','code-changes','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    for name in ('factor-start','factor-end'):
        parser.add_argument('--'+name,type=date.fromisoformat,required=True)
    args=parser.parse_args(argv)
    if args.output.exists(): raise FileExistsError('audit output already exists; immutable reports are not overwritten')
    partitions=sorted(args.daily.glob('*.parquet'))
    sources=sorted({p.resolve() for p in [*partitions,*args.cache.glob('adjust_*.json'),
        *args.stock_basic.glob('stock_basic_*.json'),args.master,args.code_changes]})
    inputs=[{'path':str(path),'sha256':_hash(path)} for path in sources]
    with duckdb.connect() as connection:
        masters=[str(row[0]) for row in connection.execute('SELECT code FROM read_parquet(?)',[str(args.master)]).fetchall()]
        rows,codes=connection.execute('SELECT count(*), count(DISTINCT code) FROM read_parquet(?)',
                                     [[str(p) for p in partitions]]).fetchone()
        priced={str(row[0]) for row in connection.execute('SELECT DISTINCT code FROM read_parquet(?)',
                                             [[str(p) for p in partitions]]).fetchall()}
    print(f'Checking all {rows} raw rows / {codes} codes; no security filter',flush=True)
    issues=[]; checked=None; loader_passed=False
    try:
        panel=load_adjusted_lake_panel(args.daily,args.cache,factor_start=args.factor_start,
            factor_end=args.factor_end,code_changes_path=args.code_changes,stock_basic_dir=args.stock_basic)
        checked=rows; loader_passed=True
        del panel
    except AdjustmentCoverageError as error:
        issues=[asdict(issue) for issue in error.issues]; checked=error.checked_bars
    except Exception as error:
        issues=[{'code':'*','reason':'loader_failed','detail':f'{type(error).__name__}: {error}'}]
    changed=[entry['path'] for entry in inputs
             if not Path(entry['path']).exists() or _hash(Path(entry['path']))!=entry['sha256']]
    missing=sorted(set(masters)-priced); unexpected=sorted(priced-set(masters))
    duplicate_master=len(masters)-len(set(masters))
    config={key:(str(value.resolve()) if isinstance(value,Path) else value.isoformat())
            for key,value in vars(args).items() if key!='output'}
    code_paths=(Path(__file__).resolve(),Path(__file__).resolve().parents[1]/'src/adjusted_panel.py')
    report={'schema':1,'config':config,'inputs':inputs,
        'code':[{'path':str(p),'sha256':_hash(p)} for p in code_paths],
        'runtime':{'python':sys.version,'duckdb':duckdb.__version__,'numpy':np.__version__},
        'raw_rows':rows,'raw_codes':codes,'raw_rows_checked':checked,
        'master_rows':len(masters),'duplicate_master_rows':duplicate_master,
        'missing_master_histories':missing,'unexpected_price_codes':unexpected,'issues':issues,
        'inputs_unchanged':not changed,'changed_inputs':changed,
        'price_gate_passed':loader_passed and not any((missing,unexpected,duplicate_master,changed)),
        'adjusted_panel_released':False,'backtest_started':False,'release_status':'NOT_RELEASED',
        'scope_limits':['Supplied master does not prove complete historical security identities.',
                        'Price checks do not validate corporate actions, investor taxes or performance.',
                        'Turnover/OHLC scope remains an upstream source-evidence requirement.']}
    canonical=json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    report['run_id']=hashlib.sha256(canonical).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,sort_keys=True,indent=2); handle.write('\n')
    print(f'raw rows checked={checked}; issues={len(issues)}; missing histories={len(missing)}; NOT_RELEASED',flush=True)
    return 0 if report['price_gate_passed'] else 1


if __name__=='__main__': raise SystemExit(main())
