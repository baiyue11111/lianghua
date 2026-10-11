"""Source-driven raw-bar investigation. No repairs, exclusions or strategy rules."""
from datetime import date
from pathlib import Path
import math

import duckdb

from adjusted_panel import _bs_code, _traded_bar


FIELDS = 'date,code,open,high,low,close,volume,amount,tradestatus'


def discrepancy_requests(daily: Path) -> list[dict]:
    paths = sorted(daily.glob('*.parquet'))
    if not paths:
        raise ValueError('no raw lake partitions')
    with duckdb.connect() as connection:
        rows = connection.execute('''
          SELECT code,min(date),max(date),count(*) FROM read_parquet(?)
          WHERE (volume>0 AND amount>0 AND
              (amount/volume<low*0.99 OR amount/volume>high*1.01))
            OR (tradestatus=0 AND (volume>0 OR amount>0))
            OR (tradestatus=1 AND (coalesce(volume,0)<=0 OR coalesce(amount,0)<=0))
            OR (tradestatus IS NULL AND coalesce(volume,0)=0 AND coalesce(amount,0)=0)
            OR open<=0 OR close<=0 OR high<low
          GROUP BY code ORDER BY code
        ''', [[str(p) for p in paths]]).fetchall()
    return [dict(method='query_history_k_data_plus',code=_bs_code(code),
                 fields=FIELDS,start_date=start,end_date=end,frequency='d',
                 adjustflag='3',affected_rows=count)
            for code,start,end,count in rows]


def validate_response(request: dict, fields: list[str], rows: list[list[str]]) -> int:
    if fields != FIELDS.split(',') or not rows:
        raise ValueError('raw response fields differ or response is empty')
    last = None
    for row in rows:
        if len(row) != len(fields):
            raise ValueError('raw response row length mismatch')
        day = date.fromisoformat(row[0])
        if (row[1] != request['code'] or not request['start_date'] <= row[0] <= request['end_date']
                or (last is not None and day <= last)):
            raise ValueError('raw response identity/date mismatch or duplicate')
        numbers = [float(value) if value else math.nan for value in row[2:8]]
        if any(math.isfinite(n) and n < 0 for n in numbers):
            raise ValueError('negative raw price/turnover')
        status = float(row[8]) if row[8] else None
        _traded_bar(status, tuple(numbers[:4]), numbers[4], numbers[5])
        if all(math.isfinite(n) for n in numbers[:4]):
            op, high, low, close = numbers[:4]
            if high < max(op,close,low) or low > min(op,close,high):
                raise ValueError('raw OHLC range inconsistent')
        last = day
    return len(rows)
