"""Independent monitoring of standard timestamped observations."""
from collections import defaultdict
from datetime import datetime, time, timezone
from math import isfinite
from statistics import median
from zoneinfo import ZoneInfo

from protocol import DataError, SQLQuery


def turnover(rows, now, *, sessions, timezone_name, window_days=20, min_samples=5,
             threshold=3, tolerance_seconds=300):
    """Compare cumulative turnover with earlier sessions at the same time."""
    zone = ZoneInfo(timezone_name)
    sessions = sorted(set(sessions))
    observations = []
    for row in rows:
        at = datetime.fromisoformat(row['data_at'])
        if at.tzinfo is None:
            raise ValueError('quote timestamp must include its timezone')
        if at > now:
            raise ValueError('quote timestamp is in the future')
        for field in ('amount', 'volume', 'price'):
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isfinite(value) or value < 0):
                raise ValueError('quote values must be finite and non-negative')
        stamp = at.astimezone(zone)
        if stamp.date().isoformat() in sessions:
            observations.append((stamp, row))
    empty = dict(at=None, price=None, volume=None, amount=None, baseline=None,
                 ratio=None, samples=0, status='unavailable')
    if not observations:
        return empty
    observations.sort(key=lambda item: item[0])
    at, latest = observations[-1]
    result = {**empty, 'at': latest['data_at'],
              **{key: latest.get(key) for key in ('price', 'volume', 'amount')}}
    if latest.get('amount') is None:
        return result
    prior = {}
    eligible = {day for day in sessions if day < at.date().isoformat()}
    eligible = set(sorted(eligible)[-window_days:])
    for stamp, row in observations:
        if stamp.date().isoformat() not in eligible or row.get('amount') is None:
            continue
        same_time = datetime.combine(stamp.date(), at.timetz())
        delta = (same_time - stamp).total_seconds()
        if 0 <= delta <= tolerance_seconds:
            prior[stamp.date()] = row['amount']
    days = sorted(prior, reverse=True)
    values = [prior[day] for day in days]
    baseline = median(values) if values else None
    result.update(samples=len(values), baseline=baseline, status='insufficient')
    if len(values) >= min_samples and baseline is not None and baseline > 0:
        ratio = latest['amount'] / baseline
        result.update(ratio=ratio, status='surge' if ratio >= threshold else 'normal')
    if (now - at).total_seconds() > tolerance_seconds:
        result['status'] = 'stale'
    return result


class Radar:
    """Read data and return indicators through its own module port."""

    def __init__(self, data):
        self.data = data

    def _reference(self, source_id):
        sources = {source['id']: source for source in self.data.registry()['sources']}
        source = sources[source_id]
        try:
            reference = self.data.source(source.get('storage_id', source['id']), 'records')
        except DataError as error:
            if error.code != 'missing':
                raise
            return None
        return reference

    def _rows(self, source_id):
        reference = self._reference(source_id)
        return self.data.table(reference).to_pylist() if reference else []

    def _quotes(self, rule, codes, start, sessions, now):
        reference = self._reference(rule['source'])
        if reference is None:
            return []
        if not reference.startswith('lake:'):
            raise ValueError('radar quotes require a standard table')
        table = '"' + reference[5:].replace('"', '""') + '"'
        marks = ','.join('?' for _ in codes)
        days = ','.join('?' for _ in sessions)
        sql = f'''WITH observed AS (
            SELECT *, timezone(?, cast(data_at AS TIMESTAMPTZ)) AS local_at
            FROM {table} WHERE code IN ({marks}) AND data_at >= ?
            AND cast(data_at AS TIMESTAMPTZ) <= cast(? AS TIMESTAMPTZ)),
        observations AS (
            SELECT * FROM observed WHERE cast(local_at AS DATE) IN ({days})),
        clocks AS (
            SELECT *, date_diff('second', date_trunc('day', local_at), local_at) AS clock
            FROM observations),
        latest AS (
            SELECT code, arg_max(clock, cast(data_at AS TIMESTAMPTZ)) AS clock,
                   max(cast(local_at AS DATE)) AS day
            FROM clocks GROUP BY code),
        comparable AS (
            SELECT o.*, row_number() OVER (
                PARTITION BY o.code, cast(o.local_at AS DATE)
                ORDER BY cast(o.data_at AS TIMESTAMPTZ) DESC) AS position
            FROM clocks o JOIN latest l ON o.code = l.code
            WHERE l.clock - o.clock BETWEEN 0 AND ?
              AND (cast(o.local_at AS DATE) = l.day OR o.amount IS NOT NULL))
        SELECT code, data_at, price, volume, amount FROM comparable WHERE position = 1'''
        return self.data.scan(SQLQuery(sql, (rule['timezone'], *codes, start, now.isoformat(), *sessions,
                                            rule['tolerance_seconds']))).table.to_pylist()

    def handle(self, operation, value):
        if operation != 'radar.scan':
            raise ValueError('unsupported radar operation: ' + operation)
        if not isinstance(value, dict) or set(value) - {'q'}:
            raise ValueError('radar scan accepts a text query')
        query = value.get('q', '')
        if not isinstance(query, str):
            raise ValueError('radar query must be text')
        query = query.strip().casefold()
        now = datetime.now(timezone.utc)
        items = []
        with self.data.transaction(readonly=True):
            rules = self.data.registry().get('radars', [])
            for rule in rules:
                params = {key: rule[key] for key in (
                    'window_days', 'min_samples', 'threshold', 'tolerance_seconds')}
                if (type(params['window_days']) is not int or params['window_days'] < 1
                        or type(params['min_samples']) is not int
                        or not 1 <= params['min_samples'] <= params['window_days']
                        or not isinstance(params['threshold'], (int, float))
                        or not isfinite(params['threshold']) or params['threshold'] <= 1
                        or type(params['tolerance_seconds']) is not int
                        or params['tolerance_seconds'] < 1):
                    raise ValueError('radar baseline settings are invalid')
                catalogue = self._rows(rule['catalogue'])
                if any(not isinstance(row.get(rule['name_field']), str)
                       or not row[rule['name_field']].strip() for row in catalogue):
                    raise ValueError('radar catalogue names must be non-empty text')
                calendar = self.data.object(rule['calendar'])
                today = now.astimezone(ZoneInfo(rule['timezone'])).date().isoformat()
                sessions = [day for day in calendar['sessions'] if day <= today]
                recent = sessions[-(params['window_days'] + 1):]
                selected = {row['code']: row[rule['name_field']] for row in catalogue
                            if not query or query in row['code'].casefold()
                            or query in str(row.get(rule['name_field'], '')).casefold()}
                grouped = defaultdict(list)
                if selected and recent:
                    start = datetime.combine(datetime.fromisoformat(recent[0]).date(), time(),
                                             ZoneInfo(rule['timezone'])).astimezone(timezone.utc)
                    rows = self._quotes(rule, tuple(selected), start.isoformat(), recent, now)
                    for row in rows:
                        grouped[row['code']].append(row)
                for code, name in selected.items():
                    result = turnover(grouped[code], now, sessions=recent,
                                      timezone_name=rule['timezone'], **params)
                    items.append({'code': code, 'name': name, **result})
        items.sort(key=lambda row: (row['status'] not in ('surge', 'normal'),
                                   row['ratio'] is None, -(row['ratio'] or 0), row['code']))
        stamps = [row['at'] for row in items if row['at']]
        return {'as_of': max(stamps, key=datetime.fromisoformat) if stamps else None, 'items': items,
                'min_samples': rules[0]['min_samples'] if rules else 5,
                'window_days': rules[0]['window_days'] if rules else 20,
                'threshold': rules[0]['threshold'] if rules else 3}
