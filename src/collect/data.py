"""Immutable source evidence, mutable job pointers and explicit human reviews."""
from __future__ import annotations
import hashlib
import json
import math
import re
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import duckdb
import numpy as np

from core.calculation import (AdjustedPricePanel, AdjustmentCoverageError, CoverageIssue,
                              FactorEvent, ListingAnchorEvidence, SourceHash)
from urllib.request import Request, urlopen


from typing import TYPE_CHECKING, Callable
if TYPE_CHECKING:
    from protocal import DataPort


def now():
    return datetime.now(timezone.utc).isoformat()

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def saved(data, kind, identity, name):
    """Absent evidence is distinct from corrupt or unreadable evidence."""
    from protocal import DataError
    try:
        return data.read(kind, identity, name)
    except DataError as error:
        if error.code != 'missing':
            raise
        return None


class Store:
    def __init__(self, data: DataPort, clock=now):
        self.data = data
        self.clock = clock
        self.lock = threading.RLock()
    @staticmethod
    def _kind(group):
        return 'sources' if group in {'snapshots','failed_evidence'} else 'state'
    def write(self, group, key, value):
        self.data.write(self._kind(group),group,f'{key}.json',encoded(value))
    def read(self, group, key):
        from protocal import DataError
        try:
            raw = self.data.read(self._kind(group), group, f'{key}.json')
        except DataError as error:
            if error.code != 'missing':
                raise
            return None
        return json.loads(raw)
    def items(self, group):
        kind=self._kind(group)
        return [json.loads(self.data.read(kind,group,ref.name))
                for ref in self.data.list(kind, group) if ref.identity==group and ref.name.endswith('.json')]
    def snapshot(self, source_id, result, request):
        value={'source_id':source_id,'collected_at':self.clock(),'request':request,**result}
        key=digest(value); value['snapshot_id']=key
        with self.lock:
            self.write('snapshots',key,value)
            self.write('latest',source_id,{'snapshot_id':key})
        return value
    def get_snapshot(self,key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key): raise ValueError('invalid snapshot identity')
        value=self.read('snapshots',key)
        if value is None: raise KeyError(key)
        if value.get('snapshot_id')!=key or digest({k:v for k,v in value.items() if k!='snapshot_id'})!=key:
            raise ValueError('snapshot hash mismatch')
        return value
    def latest(self,source_id):
        pointer=self.read('latest',source_id)
        return self.get_snapshot(pointer['snapshot_id']) if pointer else None
    def reviews(self):
        return self.items('reviews')
    def review(self,snapshot_id,record_index,status,note):
        index=record_index
        snap=self.get_snapshot(snapshot_id)
        if not 0<=index<len(snap['records']): raise ValueError('record index outside snapshot')
        if status not in {'correct','pending','anomaly'}: raise ValueError('invalid review status')
        value={'snapshot_id':snapshot_id,'record_index':index,'status':status,'note':note,'reviewed_at':self.clock()}
        self.write('reviews',f'{snapshot_id}-{index}',value)
        return value


def http(url, data=None):
    req=Request(url,data=data,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/',
                                      'Content-Type':'application/x-www-form-urlencoded'})
    with urlopen(req,timeout=25) as response: return json.loads(response.read())


class CollectionError(RuntimeError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


# --- adjusted historical data loading ---
_FACTOR_FIELDS = frozenset({
    "code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor",
})


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, read: Callable[[Path], bytes]) -> str:
    return hashlib.sha256(read(path)).hexdigest()


def _bs_code(symbol: str) -> str:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("lake code is not six digits")
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    if symbol.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return f"sz.{symbol}"
    raise ValueError("lake code is outside BaoStock's supported A-share markets")


def _factor_number(raw: object, field: str) -> float:
    if not isinstance(raw, str) or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
        raise ValueError(f"invalid {field}: {raw!r}")
    try:
        value = float(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field}: {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"non-positive or non-finite {field}: {raw!r}")
    return value


def _load_factor_cache(path: Path, symbol: str, start: date,
                       end: date, read: Callable[[Path], bytes]) -> tuple[tuple[FactorEvent, ...], SourceHash]:
    raw_file = read(path)
    payload = json.loads(raw_file)
    provider_code = _bs_code(symbol)
    request = {"kind": "adjust", "code": provider_code,
               "start": start.isoformat(), "end": end.isoformat(), "year": None}
    if not isinstance(payload, dict) or payload.get("schema") != 2:
        raise ValueError("unsupported factor-cache schema")
    if payload.get("request") != request or payload.get("request_sha256") != _sha256_json(request):
        raise ValueError("factor-cache request or request hash mismatch")
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256_json(raw):
        raise ValueError("factor-cache response hash mismatch")
    if raw.get("error_code") != "0" or raw.get("code") != provider_code:
        raise ValueError("factor-cache provider status or code mismatch")
    fields, rows = raw.get("fields"), raw.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise ValueError("factor-cache fields or rows malformed")
    if len(fields) != len(set(fields)) or not _FACTOR_FIELDS <= set(fields):
        raise ValueError("factor-cache fields missing or duplicated")
    expected_fields_hash = hashlib.sha256(",".join(fields).encode("utf-8")).hexdigest()
    if payload.get("request_fields_sha256") != expected_fields_hash:
        raise ValueError("factor-cache fields hash mismatch")
    if payload.get("status") != ("ok" if rows else "empty"):
        raise ValueError("factor-cache status and rows disagree")
    positions = {field: fields.index(field) for field in _FACTOR_FIELDS}
    events = []
    last_event = None
    for row in rows:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("factor-cache row length mismatch")
        if row[positions["code"]] != provider_code:
            raise ValueError("factor-cache row code mismatch")
        try:
            effective = date.fromisoformat(row[positions["dividOperateDate"]])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid factor effective date") from exc
        if not (start <= effective <= end) or (last_event is not None and effective <= last_event):
            raise ValueError("factor events outside coverage or not strictly ordered")
        fore = _factor_number(row[positions["foreAdjustFactor"]], "foreAdjustFactor")
        back = _factor_number(row[positions["backAdjustFactor"]], "backAdjustFactor")
        _factor_number(row[positions["adjustFactor"]], "adjustFactor")
        events.append(FactorEvent(symbol, effective, fore, back))
        last_event = effective
    return tuple(events), SourceHash(str(path), hashlib.sha256(raw_file).hexdigest())


def _load_stock_basic_anchor(path: Path, symbol: str, first_bar: date,
                             read: Callable[[Path], bytes]) -> tuple[ListingAnchorEvidence, SourceHash]:
    """Admit only an exact, one-code BaoStock IPO response for this first bar."""
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    provider_code = _bs_code(symbol)
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("unsupported stock-basic schema")
    if payload.get("request") != f"query_stock_basic({provider_code})":
        raise ValueError("stock-basic request is not exact per-code evidence")
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256_json(raw):
        raise ValueError("stock-basic raw response hash mismatch")
    if raw.get("error_code") != "0":
        raise ValueError("stock-basic provider response failed")
    fields, rows = raw.get("fields"), raw.get("rows")
    required = {"code", "ipoDate", "outDate", "type", "status"}
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise ValueError("stock-basic fields or rows malformed")
    if len(fields) != len(set(fields)) or not required <= set(fields) or len(rows) != 1:
        raise ValueError("stock-basic fields or per-code row count invalid")
    row = rows[0]
    if not isinstance(row, list) or len(row) != len(fields):
        raise ValueError("stock-basic row length mismatch")
    values = dict(zip(fields, row, strict=True))
    if values["code"] != provider_code or values["type"] != "1":
        raise ValueError("stock-basic code or security type mismatch")
    try:
        ipo_date = date.fromisoformat(values["ipoDate"])
        out_date = date.fromisoformat(values["outDate"]) if values["outDate"] else None
    except (TypeError, ValueError) as exc:
        raise ValueError("stock-basic IPO or delisting date invalid") from exc
    if out_date is not None and out_date < first_bar:
        raise ValueError("stock-basic delisting predates first bar")
    if ipo_date != first_bar:
        raise ValueError(f"stock-basic IPO {ipo_date} does not equal first raw bar {first_bar}")
    return (ListingAnchorEvidence(symbol, ipo_date, str(path), payload["raw_response_sha256"]),
            SourceHash(str(path), hashlib.sha256(file_bytes).hexdigest()))


def _load_factor_history(cache_dir: Path, symbol: str, start: date, end: date,
                         changes: tuple[dict, ...], read: Callable[[Path], bytes]):
    """Use explicit alias intervals, checking unchanged basis at each seam.

    A rename is not a distribution. A numerical discontinuity requires further
    source evidence, never automatic rescaling or a stock-specific override.
    Prefix fore factors retain their provider basis; only back factors are
    used by this signal adapter.
    """
    incoming = {}
    for change in changes:
        if change['new_code'] in incoming:
            raise ValueError('ambiguous code-change history')
        incoming[change['new_code']] = change
    def load(code, visited):
        if code in visited:
            raise ValueError('cyclic code-change history')
        path = cache_dir / f'adjust_{code}_{start}_{end}.json'
        events, source = _load_factor_cache(path, code, start, end, read)
        if code not in incoming:
            return events, (source,)
        change = incoming[code]
        if not change['factor_basis_continuity_verified']:
            raise ValueError('unverified code-change history')
        before, hashes = load(change['old_code'], visited | {code})
        cut = change['effective_date']
        prefix = tuple(event for event in before if event.effective_date < cut)
        suffix = tuple(event for event in events if event.effective_date >= cut)
        if not prefix or not suffix or suffix[0].effective_date != cut:
            raise ValueError('missing factor seam evidence')
        if not math.isclose(prefix[-1].back_factor, suffix[0].back_factor,
                            rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError('contradictory rename factor basis')
        renamed = tuple(FactorEvent(code, event.effective_date, event.fore_factor,
                                    event.back_factor) for event in prefix)
        return (renamed + suffix,
                hashes + (source,))
    return load(symbol, set())


def _load_code_changes(path: Path, read: Callable[[Path], bytes]) -> tuple[tuple[dict, ...], SourceHash]:
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("events"), list):
        raise ValueError("unsupported code-change evidence schema")
    changes = []
    for item in payload["events"]:
        if not isinstance(item, dict):
            raise ValueError("malformed code-change event")
        if not re.fullmatch(r"\d{6}", str(item.get("old_code", ""))) or not re.fullmatch(
            r"\d{6}", str(item.get("new_code", ""))
        ):
            raise ValueError("invalid old or new code in code-change evidence")
        try:
            effective_date = date.fromisoformat(item["effective_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid code-change effective date") from exc
        verified = item.get("factor_basis_continuity_verified")
        if not isinstance(verified, bool):
            raise ValueError("code-change factor-basis verdict is missing")
        changes.append({"old_code": item["old_code"], "new_code": item["new_code"],
                        "effective_date": effective_date,
                        "factor_basis_continuity_verified": verified})
    return tuple(changes), SourceHash(str(path), hashlib.sha256(file_bytes).hexdigest())


def _float_or_nan(value: object, field: str, code: str, day: str,
                  *, positive: bool) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool):
        raise ValueError(f"{code} {day}: invalid {field}: {value!r}")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0) or (not positive and number < 0):
        raise ValueError(f"{code} {day}: invalid {field}: {value!r}")
    return number


class _BarEvidenceError(ValueError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


def _traded_bar(status: object, prices: tuple[float, float, float, float],
                volume: float, amount: float) -> bool:
    """One source-status/turnover predicate for all codes and dates.

    A suspended row may carry yesterday's positive close for valuation; it is
    not an observation for MA or returns. An unknown status with no turnover
    is not silently classified as either a trade or a suspension.
    """
    if status is not None:
        if isinstance(status, bool) or status not in (0, 0.0, 1, 1.0):
            raise _BarEvidenceError("invalid_trade_status", f"status={status!r}")
        if status == 0:
            if (math.isfinite(volume) and volume > 0) or (math.isfinite(amount) and amount > 0):
                raise _BarEvidenceError("contradictory_trade_state",
                                        f"status=0, volume={volume}, amount={amount}")
            return False
    if math.isfinite(volume) and volume > 0 and math.isfinite(amount) and amount > 0:
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise _BarEvidenceError("invalid_lake_price", f"traded OHLC={prices}")
        # RMB turnover / shares must lie inside the raw daily trading range.
        # A 1% tolerance absorbs source rounding, not a change of price basis.
        vwap = amount / volume
        if not prices[2] * 0.99 <= vwap <= prices[1] * 1.01:
            raise _BarEvidenceError("inconsistent_price_basis",
                                    f"VWAP={vwap}, low={prices[2]}, high={prices[1]}")
        return True
    if status == 1:
        raise _BarEvidenceError("contradictory_trade_state",
                                f"status=1, volume={volume}, amount={amount}")
    if ((not math.isfinite(volume) or volume == 0)
            and (not math.isfinite(amount) or amount == 0)):
        raise _BarEvidenceError("unknown_trade_state",
                                f"status missing, volume={volume}, amount={amount}, OHLC={prices}")
    raise _BarEvidenceError("contradictory_trade_state",
                            f"status missing, volume={volume}, amount={amount}")


def load_adjusted_lake_panel(
    daily_dir: Path,
    cache_dir: Path,
    *,
    factor_start: date,
    factor_end: date,
    code_changes_path: Path,
    stock_basic_dir: Path | None = None,
    read: Callable[[Path], bytes],
    list_files: Callable[[Path, str], tuple[Path, ...]],
) -> AdjustedPricePanel:
    """Load the *entire* supplied A-share lake with fail-closed factor coverage.

    A first bar preceding the first event is anchored at BaoStock's documented
    listing-day backward factor 1 only when an exact per-code, hash-validated
    stock-basic snapshot confirms ``ipoDate == first raw bar`` and the supplied
    code-change evidence has no unresolved rename for that history. A global
    all-stock response cannot substitute for the per-code snapshot because
    BaoStock may silently truncate it while returning success.
    """
    daily_dir, cache_dir = Path(daily_dir), Path(cache_dir)
    if not isinstance(factor_start, date) or not isinstance(factor_end, date) or factor_start > factor_end:
        raise ValueError("valid factor_start and factor_end dates are required")
    paths = sorted(list_files(daily_dir, "*.parquet"))
    if not paths:
        raise AdjustmentCoverageError((CoverageIssue("*", "missing_lake", str(daily_dir)),))
    lake_hashes = tuple(SourceHash(str(path), _sha256_file(path, read)) for path in paths)
    try:
        changes, identity_hash = _load_code_changes(Path(code_changes_path), read)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AdjustmentCoverageError((CoverageIssue(
            "*", "invalid_code_change_evidence", str(exc)),)) from exc
    connection = duckdb.connect()
    files = [str(path) for path in paths]
    try:
        bounds = connection.execute(
            "SELECT code, min(date), max(date), count(*), count(DISTINCT date), "
            "arg_max(name, date) FROM read_parquet(?) GROUP BY code ORDER BY code",
            [files],
        ).fetchall()
        calendar_strings = [row[0] for row in connection.execute(
            "SELECT DISTINCT date FROM read_parquet(?) ORDER BY date", [files]
        ).fetchall()]
        if not bounds or not calendar_strings:
            raise AdjustmentCoverageError((CoverageIssue("*", "empty_lake", str(daily_dir)),))
        issues: list[CoverageIssue] = []
        symbols: list[str] = []
        names: list[str] = []
        factor_events: list[FactorEvent] = []
        listing_anchors: list[ListingAnchorEvidence] = []
        cache_hashes: list[SourceHash] = []
        stock_basic_hashes: list[SourceHash] = []
        events_by_code: dict[str, tuple[FactorEvent, ...]] = {}
        listing_anchor_codes: set[str] = set()
        for code, first_string, last_string, count, distinct_count, name in bounds:
            code = str(code)
            symbols.append(code)
            names.append(str(name))
            if count != distinct_count:
                issues.append(CoverageIssue(code, "duplicate_lake_bar",
                                            f"{count - distinct_count} duplicate code-date rows"))
            try:
                provider_code = _bs_code(code)
                first = date.fromisoformat(str(first_string))
                last = date.fromisoformat(str(last_string))
            except (TypeError, ValueError) as exc:
                issues.append(CoverageIssue(code, "invalid_lake_identity_or_date", str(exc)))
                continue
            if first < factor_start or last > factor_end:
                issues.append(CoverageIssue(code, "outside_factor_query_window",
                                            f"bars {first}..{last}, query {factor_start}..{factor_end}"))
                continue
            unresolved = [change for change in changes
                          if not change["factor_basis_continuity_verified"]
                          and ((change["new_code"] == code and first < change["effective_date"])
                               or (change["old_code"] == code and last >= change["effective_date"]))]
            if unresolved:
                change = unresolved[0]
                issues.append(CoverageIssue(
                    code, "unverified_code_change",
                    f"{change['old_code']}->{change['new_code']} on {change['effective_date']}"))
                continue
            cache_path = cache_dir / f"adjust_{code}_{factor_start}_{factor_end}.json"
            try:
                events, history_hashes = _load_factor_history(
                    cache_dir, code, factor_start, factor_end, changes, read)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                reason = "missing_factor_cache" if isinstance(exc, FileNotFoundError) else (
                    "invalid_factor_history" if "seam" in str(exc) or "rename" in str(exc)
                    or "code-change" in str(exc) else "invalid_factor_cache")
                issues.append(CoverageIssue(code, reason, f"{cache_path}: {exc}"))
                continue
            cache_hashes.extend(history_hashes)
            if not events:
                issues.append(CoverageIssue(code, "missing_initial_anchor",
                                            f"first raw bar {first}, no factor events"))
                continue
            if events[0].effective_date > first:
                if stock_basic_dir is None:
                    issues.append(CoverageIssue(
                        code, "missing_initial_anchor",
                        "per-code stock-basic and code-change evidence are required"))
                    continue
                stock_basic_path = Path(stock_basic_dir) / f"stock_basic_{provider_code}.json"
                try:
                    anchor, source_hash = _load_stock_basic_anchor(stock_basic_path, code, first, read)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    reason = ("missing_stock_basic_snapshot" if isinstance(exc, FileNotFoundError)
                              else "invalid_stock_basic_snapshot" if "hash" in str(exc) or
                              "schema" in str(exc) or "request" in str(exc) or "malformed" in str(exc)
                              else "missing_initial_anchor")
                    issues.append(CoverageIssue(code, reason, f"{stock_basic_path}: {exc}"))
                    continue
                listing_anchors.append(anchor)
                stock_basic_hashes.append(source_hash)
                listing_anchor_codes.add(code)
            events_by_code[code] = events
            factor_events.extend(events)
        calendar = None
        try:
            calendar = tuple(date.fromisoformat(str(value)) for value in calendar_strings)
        except (TypeError, ValueError) as exc:
            issues.append(CoverageIssue('*', 'invalid_lake_date', str(exc)))
        symbol_tuple = tuple(symbols)
        arrays = None
        traded_bar = None
        # Failed factor/identity evidence still needs independent bar auditing,
        # but cannot produce a panel. Do not allocate its dense price matrices.
        if not issues:
            shape = (len(symbol_tuple), len(calendar))
            arrays = {field: np.full(shape, np.nan, dtype=np.float64)
                      for field in ("open", "high", "low", "close", "volume", "amount")}
            traded_bar = np.zeros(shape, dtype=bool)
        bar_issue_counts: dict[tuple[str, str], int] = {}
        bar_issue_examples: dict[tuple[str, str], str] = {}
        checked_bars = 0
        code_index = {code: row for row, code in enumerate(symbol_tuple)}
        date_index = {str(day): column for column, day in enumerate(calendar_strings)}
        reader = connection.execute(
            "SELECT code, date, open, high, low, close, volume, amount, tradestatus "
            "FROM read_parquet(?) ORDER BY code, date", [files],
        ).to_arrow_reader(batch_size=100_000)
        for batch in reader:
            columns = batch.to_pydict()
            for values in zip(*(columns[field] for field in
                                ("code", "date", "open", "high", "low", "close", "volume", "amount", "tradestatus")),
                              strict=True):
                code, day, raw_open, raw_high, raw_low, raw_close, volume, amount, status = values
                checked_bars += 1
                code, day = str(code), str(day)
                try:
                    numbers = {field: _float_or_nan(value, field, code, day, positive=False)
                               for field, value in (
                        ("open", raw_open), ("high", raw_high),
                        ("low", raw_low), ("close", raw_close),
                        ("volume", volume), ("amount", amount),
                    )}
                    prices = tuple(numbers[field] for field in
                                   ("open", "high", "low", "close"))
                    traded = _traded_bar(status, prices, numbers['volume'], numbers['amount'])
                    if arrays is not None:
                        row, column = code_index[code], date_index[day]
                        for field, number in numbers.items(): arrays[field][row, column] = number
                        traded_bar[row, column] = traded
                except _BarEvidenceError as exc:
                    reason, detail = exc.reason, str(exc)
                except (TypeError, ValueError, OverflowError) as exc:
                    reason, detail = "invalid_lake_price", str(exc)
                else:
                    continue
                key = (code, reason)
                bar_issue_counts[key] = bar_issue_counts.get(key, 0) + 1
                bar_issue_examples.setdefault(key, f"{day}: {detail}")
        if bar_issue_counts:
            issues.extend(
                CoverageIssue(code, reason,
                              f"{count} row(s); example {bar_issue_examples[(code, reason)]}")
                for (code, reason), count in sorted(bar_issue_counts.items())
            )
        for source in lake_hashes:
            if _sha256_file(Path(source.path), read) != source.sha256:
                issues.append(CoverageIssue("*", "lake_changed_during_load", source.path))
        if issues:
            raise AdjustmentCoverageError(tuple(issues), checked_bars=checked_bars)

        factor = np.full(shape, np.nan, dtype=np.float64)
        ordinal_calendar = np.asarray([day.toordinal() for day in calendar], dtype=np.int64)
        for row, code in enumerate(symbol_tuple):
            events = events_by_code[code]
            dates = np.asarray([event.effective_date.toordinal() for event in events], dtype=np.int64)
            values = np.asarray([event.back_factor for event in events], dtype=np.float64)
            positions = np.searchsorted(dates, ordinal_calendar, side="right") - 1
            valid = (positions >= 0) & np.isfinite(arrays["close"][row])
            factor[row, valid] = values[positions[valid]]
            if code in listing_anchor_codes:
                before_first_event = (positions < 0) & np.isfinite(arrays["close"][row])
                factor[row, before_first_event] = 1.0
        priced = np.isfinite(arrays["close"])
        unanchored = priced & ~np.isfinite(factor)
        if unanchored.any():
            row, column = np.argwhere(unanchored)[0]
            raise AdjustmentCoverageError((CoverageIssue(
                symbol_tuple[row], "missing_bar_factor", str(calendar[column])),))
        return AdjustedPricePanel(
            calendar=calendar, symbols=symbol_tuple, names=tuple(names),
            raw_open=arrays["open"], raw_high=arrays["high"],
            raw_low=arrays["low"], raw_close=arrays["close"],
            volume=arrays["volume"], amount=arrays["amount"],
            back_factor=factor, traded_bar=traded_bar,
            signal_close=np.where(traded_bar, arrays["close"] * factor, np.nan),
            signal_high=np.where(traded_bar, arrays["high"] * factor, np.nan),
            factor_events=tuple(factor_events),
            listing_anchors=tuple(listing_anchors),
            source_hashes=lake_hashes + tuple(cache_hashes) + tuple(stock_basic_hashes)
            + (identity_hash,),
        )
    finally:
        connection.close()
