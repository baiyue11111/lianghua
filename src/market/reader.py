"""Read standard source facts into point-in-time datasets."""
from __future__ import annotations

import duckdb
import hashlib
import json
import math
import numpy as np
import re
from collections import deque
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from protocol import (
    Bar,
    Dataset,
    EXECUTION_FIELDS,
    HistoryBar,
    HistoryDay,
    LIQUIDITY_WINDOW,
    MarketFrame,
    RegistrationAction,
    _calendar_covers,
    _calendar_facts,
    normalize_security_id,
    validate_facts,
)
from typing import Callable
from zoneinfo import ZoneInfo

def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, read: Callable[[Path], bytes]) -> str:
    return hashlib.sha256(read(path)).hexdigest()


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
    from core.calculation import FactorEvent, SourceHash
    raw_file = read(path)
    payload = json.loads(raw_file)
    code = normalize_security_id(symbol)
    rows, _ = validate_facts(payload, kind="adjust", code=code,
                              start=start.isoformat(), end=end.isoformat(), year=None)
    events = []
    last_event = None
    for row in rows:
        effective = date.fromisoformat(row["date"])
        fore = float(row["forward"])
        back = float(row["backward"])
        events.append(FactorEvent(symbol, effective, fore, back))
        last_event = effective
    return tuple(events), SourceHash(str(path), hashlib.sha256(raw_file).hexdigest())


def _load_stock_basic_anchor(path: Path, symbol: str, first_bar: date,
                             read: Callable[[Path], bytes]) -> tuple[ListingAnchorEvidence, SourceHash]:
    from core.calculation import ListingAnchorEvidence, SourceHash
    """Use a normalized security fact as listing-anchor evidence."""
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    code = normalize_security_id(symbol)
    rows, source_hash = validate_facts(
        payload, kind="security", code=code, start=None, end=None, year=None)
    row = rows[0]
    if row["type"] != "1":
        raise ValueError("security fact type or status is invalid")
    ipo_date = date.fromisoformat(row["listing_date"])
    out_date = None if row["delisting_date"] is None else date.fromisoformat(row["delisting_date"])
    if out_date is not None and out_date < first_bar:
        raise ValueError("security delisting predates first bar")
    if ipo_date != first_bar:
        raise ValueError(f"security listing {ipo_date} does not equal first raw bar {first_bar}")
    return (ListingAnchorEvidence(symbol, ipo_date, str(path), source_hash),
            SourceHash(str(path), hashlib.sha256(file_bytes).hexdigest()))


def _load_factor_history(cache_dir: Path, symbol: str, start: date, end: date,
                         changes: tuple[dict, ...], read: Callable[[Path], bytes],
                         template: str = 'adjust_{symbol}_{start}_{end}.json'):
    from core.calculation import FactorEvent
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
        path = cache_dir / template.format(symbol=normalize_security_id(code), start=start, end=end)
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
    from core.calculation import SourceHash
    file_bytes = read(path)
    payload = json.loads(file_bytes)
    if not isinstance(payload, dict) or payload.get("schema") != 1 or not isinstance(payload.get("events"), list):
        raise ValueError("unsupported code-change evidence schema")
    changes = []
    outgoing = {}
    incoming = set()
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
        old_code, new_code = item["old_code"], item["new_code"]
        if old_code == new_code or old_code in outgoing or new_code in incoming:
            raise ValueError("ambiguous code-change graph")
        change = {"old_code": old_code, "new_code": new_code,
                  "effective_date": effective_date,
                  "factor_basis_continuity_verified": verified}
        outgoing[old_code] = change
        incoming.add(new_code)
        changes.append(change)
    for start in outgoing:
        code, previous_date, visited = start, None, set()
        while code in outgoing:
            if code in visited:
                raise ValueError("cyclic code-change graph")
            visited.add(code)
            change = outgoing[code]
            if previous_date is not None and change["effective_date"] <= previous_date:
                raise ValueError("reverse code-change dates")
            previous_date = change["effective_date"]
            code = change["new_code"]
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


def _parquet_paths_for_dates(paths: tuple[Path, ...], start: date, end: date) -> tuple[Path, ...]:
    import pyarrow.parquet as pq

    selected=[]
    for path in paths:
        parquet=pq.ParquetFile(path)
        column=parquet.schema_arrow.get_field_index('date')
        if column < 0:
            raise ValueError(f'parquet date field missing: {path}')
        intersects=False
        for index in range(parquet.metadata.num_row_groups):
            stats=parquet.metadata.row_group(index).column(column).statistics
            if stats is None or not stats.has_min_max:
                intersects=True
                break
            low,high=stats.min,stats.max
            low=low.date() if isinstance(low,datetime) else low
            high=high.date() if isinstance(high,datetime) else high
            if not isinstance(low,date):
                low=date.fromisoformat(str(low))
            if not isinstance(high,date):
                high=date.fromisoformat(str(high))
            if low<=end and high>=start:
                intersects=True
                break
        if intersects:
            selected.append(path)
    return tuple(selected)


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
            raise _BarEvidenceError("invalid_source_price", f"traded OHLC={prices}")
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


def _load_adjusted_panel(
    daily_dir: Path,
    cache_dir: Path,
    *,
    factor_start: date,
    factor_end: date,
    code_changes_path: Path,
    stock_basic_dir: Path | None = None,
    symbols: tuple[str, ...] | None = None,
    date_start: date | None = None,
    date_end: date | None = None,
    factor_template: str = 'adjust_{symbol}_{start}_{end}.json',
    stock_basic_template: str = 'stock_basic_{provider_code}.json',
    read: Callable[[Path], bytes],
    list_files: Callable[[Path, str], tuple[Path, ...]],
) -> AdjustedPricePanel:
    from core.calculation import (AdjustedPricePanel, AdjustmentCoverageError, CoverageIssue,
                                  SourceHash)
    """Load the *entire* supplied A-share source with fail-closed factor coverage.

    A first bar preceding the first adjustment requires a verified listing date
    equal to that bar and no unresolved security code change. Listing identity
    and adjustment coverage come from standard fact records.
    """
    daily_dir, cache_dir = Path(daily_dir), Path(cache_dir)
    if not isinstance(factor_start, date) or not isinstance(factor_end, date) or factor_start > factor_end:
        raise ValueError("valid factor_start and factor_end dates are required")
    paths = sorted(list_files(daily_dir, "*.parquet"))
    if date_start is not None and date_end is not None:
        paths=list(_parquet_paths_for_dates(tuple(paths),date_start,date_end))
    if not paths:
        raise AdjustmentCoverageError((CoverageIssue("*", "missing_source", str(daily_dir)),))
    connection = duckdb.connect()
    source_hashes = tuple(SourceHash(str(path), _sha256_file(path, read)) for path in paths)
    try:
        changes, identity_hash = _load_code_changes(Path(code_changes_path), read)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AdjustmentCoverageError((CoverageIssue(
            "*", "invalid_code_change_evidence", str(exc)),)) from exc
    files = [str(path) for path in paths]
    # Resolve the storage key before selection and grouping. Price arithmetic
    # uses the A-share number; the dataset port restores the requested identity.
    source = ("(SELECT split_part(identity, '.', 2) AS code, * EXCLUDE(code, identity) "
              "FROM (SELECT *, CASE WHEN contains(code, '.') THEN lower(code) "
              "WHEN starts_with(code, '6') THEN 'sh.' || code ELSE 'sz.' || code END AS identity "
              "FROM read_parquet(?, union_by_name=true))")
    query_params = [files]
    if symbols is not None:
        source += " WHERE identity IN (SELECT * FROM UNNEST(?))"
        query_params.append([normalize_security_id(symbol) for symbol in symbols])
    source += ")"
    code_filter = ""
    date_filter = " AND " if code_filter else " WHERE "
    if date_start is not None:
        date_filter += "CAST(date AS DATE) >= ?"
        query_params.append(date_start)
    if date_end is not None:
        date_filter += (" AND " if date_start is not None else "") + "CAST(date AS DATE) <= ?"
        query_params.append(date_end)
    if date_start is None and date_end is None:
        date_filter = ""
    row_filter = code_filter + date_filter
    try:
        bounds = connection.execute(
            "SELECT code, min(date), max(date), count(*), count(DISTINCT date), "
            "arg_max(name, date) FROM " + source + row_filter + " GROUP BY code ORDER BY code",
            query_params,
        ).fetchall()
        calendar_strings = [row[0] for row in connection.execute(
            "SELECT DISTINCT date FROM " + source + row_filter + " ORDER BY date", query_params
        ).fetchall()]
        if not bounds or not calendar_strings:
            raise AdjustmentCoverageError((CoverageIssue("*", "empty_source", str(daily_dir)),))
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
                issues.append(CoverageIssue(code, "duplicate_source_bar",
                                            f"{count - distinct_count} duplicate code-date rows"))
            try:
                market_code = normalize_security_id(code)
                first = date.fromisoformat(str(first_string))
                last = date.fromisoformat(str(last_string))
            except (TypeError, ValueError) as exc:
                issues.append(CoverageIssue(code, "invalid_source_identity_or_date", str(exc)))
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
            cache_path = cache_dir / factor_template.format(
                symbol=normalize_security_id(code), start=factor_start, end=factor_end)
            try:
                events, history_hashes = _load_factor_history(
                    cache_dir, code, factor_start, factor_end, changes, read,
                    factor_template)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                reason = "missing_factor_cache" if isinstance(exc, FileNotFoundError) else "invalid_factor_history"
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
                stock_basic_path = Path(stock_basic_dir) / stock_basic_template.format(
                    symbol=normalize_security_id(code), provider_code=market_code)
                try:
                    anchor, source_hash = _load_stock_basic_anchor(stock_basic_path, code, first, read)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    reason = ("missing_stock_basic_snapshot" if isinstance(exc, FileNotFoundError)
                              else "missing_initial_anchor" if "does not equal first raw bar" in str(exc)
                              else "invalid_stock_basic_snapshot")
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
            issues.append(CoverageIssue('*', 'invalid_source_date', str(exc)))
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
            "FROM " + source + row_filter + " ORDER BY code, date", query_params,
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
                    reason, detail = "invalid_source_price", str(exc)
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
        for source in source_hashes:
            if _sha256_file(Path(source.path), read) != source.sha256:
                issues.append(CoverageIssue("*", "source_changed_during_load", source.path))
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
            source_hashes=source_hashes + tuple(cache_hashes) + tuple(stock_basic_hashes)
            + (identity_hash,),
        )
    finally:
        connection.close()


def _load_dataset(data, query):
    from core.calculation import (normalize_dividend_records, reconcile_corporate_actions,
                                  TaxLedger, FeePolicy, TaxPolicy)
    from strategy import canonical_hash
    provided={'open','high','low','close','volume','amount','signal_close'}
    if query.frequency!='daily' or query.price_basis!='adjusted':
        raise ValueError('historical adapter supports daily adjusted signal data only')
    dataset_id=query.dataset
    folder=data.path('sources',dataset_id,'manifest.json').parent
    def safe_path(root, name):
        path=(root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('historical evidence path escapes snapshot')
        return path
    def shared_ref(name):
        parts=Path(name).parts
        if not parts or parts[0]!='source':
            return None
        if len(parts)<2 or any(part in ('','.','..') for part in parts):
            raise ValueError('invalid shared source reference')
        identity=parts[1]
        relative=Path(*parts[2:]).as_posix() if len(parts)>2 else ''
        return identity,relative
    def source_ref(name):
        shared=shared_ref(name)
        if shared is not None:
            identity,relative=shared
            if not relative:
                return data.path('sources',identity,'_').parent, identity, relative
            return data.path('sources',identity,relative), identity, relative
        path=safe_path(folder,name)
        return path, dataset_id, path.relative_to(folder).as_posix()
    def related_name(base, child):
        if shared_ref(child) is not None:
            return child
        _, identity, relative = source_ref(base)
        parent = Path(relative).parent
        joined = (parent / child).as_posix()
        if identity == dataset_id:
            return joined
        return f"source/{identity}/{joined}"
    def read(name):
        _, identity, relative=source_ref(name)
        return data.read('sources',identity,relative)
    def refs_under(directory):
        shared=shared_ref(directory)
        if shared is not None:
            identity,relative=shared
            prefix=relative.rstrip('/')+'/' if relative else ''
            return tuple(ref for ref in data.list('sources',identity)
                         if ref.name.startswith(prefix))
        prefix=directory.rstrip('/')+'/'
        return tuple(ref for ref in data.list('sources',dataset_id)
                     if ref.name.startswith(prefix))
    def ref_name(directory, name):
        shared=shared_ref(directory)
        if shared is not None:
            identity,_=shared
            return f"source/{identity}/{Path(name).as_posix()}"
        return name
    manifest=data.manifest(dataset_id)
    overlap=set(manifest.get('fields',{}))&provided
    if overlap: raise ValueError('reserved price fields cannot be redefined: '+', '.join(sorted(overlap)))
    scope=manifest['scope']
    if not isinstance(scope['universe'],str) or not scope['universe'].strip(): raise ValueError('explicit dataset universe scope required')
    if query.universe!='dataset' and query.universe!=scope['universe']:
        raise ValueError(f'universe_scope_mismatch: 策略要求 {query.universe}；数据仅为 {scope["universe"]}，不能替代全市场。')
    extra={name:manifest['fields'][name] for name in set(query.fields)-provided
           if name in manifest.get('fields',{})}
    missing=set(query.fields)-provided-set(extra)
    if missing: raise ValueError('unsupported_fields: '+', '.join(sorted(missing)))
    for name,spec in extra.items():
        if (set(spec)!={'column','unit'} or any(not isinstance(spec[key],str) or not spec[key].strip() for key in spec)):
            raise ValueError('declared numeric field column and unit required: '+name)

    refs=manifest['files']
    if not isinstance(refs,list) or not refs: raise ValueError('historical evidence files are required')
    bound_names=set()
    hashes={}
    names_by_path={}
    market_files={}
    account_files={}
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get('file'), str):
            raise ValueError('historical evidence file reference is malformed')
        if ref['file'] in bound_names:
            raise ValueError('duplicate historical evidence file')
        bound_names.add(ref['file'])
    def evidence(name):
        path, _, _ = source_ref(name)
        if str(path) not in hashes:
            hashes[str(path)]=sha256(read(name)).hexdigest()
            names_by_path[str(path)]=name
        account_files[str(path)]=hashes[str(path)]
        return path
    def market_evidence(name):
        path=evidence(name)
        market_files[str(path)]=hashes[str(path)]
        return path
    inputs=manifest.get('inputs')
    if not isinstance(inputs,dict) or not isinstance(inputs.get('calendar'),str):
        raise ValueError('calendar input must name a calendar file')
    calendar_name=inputs['calendar']
    market_evidence(calendar_name)
    calendar_payload=json.loads(read(calendar_name))
    calendar, coverage = _calendar_facts(calendar_payload)
    calendar_start=coverage[0][0]
    calendar_end=coverage[-1][1]
    bars_dir=inputs['bars']
    adjustments=inputs['adjustments']
    factor_dir=adjustments['template'].rsplit('/', 1)[0]
    securities=inputs['securities']
    stock_dir=securities['template'].rsplit('/', 1)[0]
    changes_name=inputs['code_changes']
    policy_name=inputs['account_policy']
    factor_start=date.fromisoformat(adjustments['start'])
    factor_end=date.fromisoformat(adjustments['end'])
    declared=tuple(scope.get('symbols', ()))
    declared_ids=tuple(normalize_security_id(symbol) for symbol in declared)
    if len(set(declared_ids)) != len(declared_ids):
        raise ValueError('declared security scope contains duplicate identities')
    requested_ids=None if query.symbols is None else tuple(
        normalize_security_id(symbol) for symbol in query.symbols)
    if requested_ids is not None and len(set(requested_ids)) != len(requested_ids):
        raise ValueError('requested symbols contain duplicate identities')
    if requested_ids is not None and declared_ids and not set(requested_ids) <= set(declared_ids):
        raise ValueError('requested symbols are outside supplied dataset')
    if query.universe!='dataset' and scope['universe']!=query.universe:
        raise ValueError('universe_scope_mismatch: dataset scope does not satisfy the requested universe')
    if query.start < calendar[0] or query.end > calendar[-2]:
        raise ValueError('requested interval is outside historical snapshot')
    if query.universe!='dataset' and requested_ids is not None and set(requested_ids)!=set(declared_ids):
        raise ValueError('universe_scope_mismatch: requested subset does not satisfy complete universe')
    from bisect import bisect_left, bisect_right
    start_index=bisect_left(calendar,query.start)
    end_index=bisect_right(calendar,query.end)-1
    if start_index >= len(calendar)-1 or end_index < start_index:
        raise ValueError('requested interval contains no trading sessions')
    warmup=max(query.history,LIQUIDITY_WINDOW)
    if start_index < warmup:
        raise ValueError(f'insufficient pre-start history: requires {warmup} sessions (strategy and execution)')
    read_start=calendar[start_index-warmup]
    actual_start=calendar[start_index]
    actual_end=calendar[end_index]
    plugins=manifest.get('signal_plugins',{})
    if not isinstance(plugins,dict):
        raise ValueError('signal_plugins must be a mapping')
    scaler=plugins.get('scaler')
    if scaler is not None and isinstance(scaler,dict) and isinstance(scaler.get('fit_start'),str):
        fit_start=date.fromisoformat(scaler['fit_start'])
        fit_index=bisect_left(calendar,fit_start)
        if fit_index >= len(calendar)-1:
            raise ValueError('signal scaler fit window is not fully covered')
        read_start=calendar[min(start_index-warmup,fit_index)]
    read_start_index=calendar.index(read_start)
    read_end=actual_end
    sentinel=calendar[end_index+1]
    if not _calendar_covers(coverage, read_start, sentinel):
        raise ValueError('calendar coverage does not cover selected warm-up, interval, and sentinel')
    external_roots={}
    for directory in (bars_dir, factor_dir, stock_dir):
        shared=shared_ref(directory)
        if shared is not None:
            identity,_=shared
            external_roots[identity]=data.path('sources',identity,'_').parent
    def read_path(path):
        if path.is_relative_to(folder):
            identity=dataset_id
            relative=path.relative_to(folder).as_posix()
            name=relative
        else:
            source_root=data.path('sources','_','_').parents[1]
            if not path.is_relative_to(source_root):
                raise ValueError('unbound historical evidence path')
            relative_path=path.relative_to(source_root)
            if len(relative_path.parts)<2:
                raise ValueError('unbound historical evidence path')
            identity=relative_path.parts[0]
            relative=Path(*relative_path.parts[1:]).as_posix()
            name=f'source/{identity}/{relative}'
        payload=data.read('sources',identity,relative)
        digest=sha256(payload).hexdigest()
        hashes[str(path)]=digest
        names_by_path[str(path)]=name
        market_files[str(path)]=digest
        return payload
    def list_files(directory, pattern):
        shared=shared_ref(directory)
        if shared is not None:
            identity,relative=shared
            prefix=relative.rstrip('/')+'/' if relative else ''
            return tuple(data.path('sources',identity,ref.name)
                         for ref in data.list('sources',identity)
                         if ref.name.startswith(prefix)
                         and ref.name.endswith(pattern.removeprefix('*')))
        for identity, root in external_roots.items():
            if Path(directory).is_relative_to(root):
                prefix=Path(directory).relative_to(root).as_posix().rstrip('/')+'/'
                return tuple(data.path('sources',identity,ref.name)
                             for ref in data.list('sources',identity)
                             if ref.name.startswith(prefix)
                             and ref.name.endswith(pattern.removeprefix('*')))
        prefix=Path(directory).relative_to(folder).as_posix().rstrip('/')+'/'
        return tuple(data.path('sources',dataset_id,ref.name)
                     for ref in data.list('sources',dataset_id)
                     if ref.name.startswith(prefix)
                     and ref.name.endswith(pattern.removeprefix('*')))
    selected_symbols=(declared if requested_ids is None and declared else
                      None if requested_ids is None else
                      tuple(symbol.split('.')[-1] for symbol in requested_ids))
    panel=_load_adjusted_panel(
        source_ref(bars_dir)[0],source_ref(factor_dir)[0],
        factor_start=factor_start,factor_end=factor_end,
        code_changes_path=market_evidence(changes_name),stock_basic_dir=source_ref(stock_dir)[0],
        symbols=selected_symbols,date_start=read_start,date_end=read_end,
        factor_template=Path(adjustments['template']).name,
        stock_basic_template=Path(securities['template']).name,
        read=read_path,
        list_files=list_files)
    scope = dict(scope)
    scope['calendar_start'] = calendar_start.isoformat()
    scope['calendar_end'] = calendar[-2].isoformat()
    if requested_ids is None and declared_ids and set(normalize_security_id(symbol) for symbol in panel.symbols) != set(declared_ids):
        raise ValueError('declared security scope differs from supplied historical source')
    if tuple(calendar[read_start_index:end_index+1]) != panel.calendar:
        raise ValueError('price dates do not cover selected warm-up and query interval')
    start,end=query.start,query.end
    as_of=query.as_of
    if as_of is not None and as_of < actual_end:
        raise ValueError('data as_of precedes query end')
    if as_of is not None and factor_end != as_of:
        raise ValueError('point-in-time factor snapshot must end exactly at query cutoff')
    calendar = tuple(calendar[read_start_index:end_index+2])
    # Each requested code/year cache is explicit evidence, including successful empty results.
    coverage_template=inputs['distributions']['template']
    source_events={}; query_keys=set(); observations=[]; facts={}
    selected_codes=tuple(panel.symbols)
    for code in selected_codes:
      for year in range(read_start.year, actual_end.year + 1):
        name=coverage_template.format(symbol=normalize_security_id(code),year=year)
        path=market_evidence(name); payload=json.loads(read(name))
        records, _ = validate_facts(payload, kind='dividend',
            code=normalize_security_id(code), start=None, end=None, year=str(year))
        query_keys.add((code,year))
        for row,record in enumerate(records):
            try:
                event=normalize_dividend_records([{**record,'code':normalize_security_id(code)}])[0]
            except ValueError as error:
                raise ValueError(f'{name}, record {row}: {error}') from error
            event=replace(event,code=code)
            if event.registration_date is not None and read_start<=event.registration_date<=actual_end:
                if not event.executable: raise ValueError('corporate action settlement evidence incomplete')
                if not record.get('announce_date') or not record.get('registration_date'):
                    raise ValueError('cash distribution identity dates are required')
                distribution_id=f"{code}:{record['announce_date']}:{record['registration_date']}"
                source=canonical_hash({'file':name,'row':row,'hash':hashes[str(path)]})
                observations.append((source,distribution_id,event))
                source_events[(name,row)]=(event,hashes[str(path)])
                cash_only=(event.stock_dividend_per_share == 0 and event.reserve_to_stock_per_share == 0)
                if cash_only:
                    facts[(code,distribution_id)]=((None,Decimal('1'),Decimal('0')),
                        [(hashes[str(path)],hashes[str(path)])])
                else:
                    for item in inputs.get('action_facts') or ():
                        if item.get('source_file') == name and item.get('source_row') == row:
                            credit_date=item.get('credit_date')
                            credit_source=item.get('credit_source_file')
                            if not isinstance(credit_date,str) or not credit_date.strip():
                                raise ValueError('noncash corporate action requires a valid credit date')
                            try:
                                parsed_credit_date=date.fromisoformat(credit_date)
                            except ValueError as error:
                                raise ValueError('noncash corporate action credit date is invalid') from error
                            if parsed_credit_date.isoformat()!=credit_date:
                                raise ValueError('noncash corporate action credit date is invalid')
                            if not isinstance(credit_source,str) or not credit_source.strip():
                                raise ValueError('noncash corporate action requires a credit source file')
                            terms=(item.get('credit_date'),Decimal(item['stock_nominal_per_share']),
                                   Decimal(item['reserve_taxable_per_share']))
                            facts[(code,distribution_id)]=(terms,[(hashes[str(path)],hashes[str(evidence(item['tax_source_file']))])])
                            market_evidence(credit_source)
                            break
                    if (code,distribution_id) not in facts:
                        raise ValueError('noncash corporate action requires sourced credit and tax facts')
    policy_file=evidence(policy_name)
    policy=json.loads(read(policy_name))
    if policy['schema']!=1 or policy['investor']!='domestic_individual_unrestricted_a_share':
        raise ValueError('unsupported investor account policy')
    for source in policy['sources']:
        source_name=related_name(policy_name,source['file'])
        path=evidence(source_name)
        if hashes[str(path)]!=source['sha256']: raise ValueError('policy source hash mismatch')
    policy_hash=hashes[str(policy_file)]
    fees=FeePolicy.from_mapping(policy['fees'],policy_hash)
    taxes=TaxPolicy.from_mapping(policy['tax'],policy_hash)
    if start<taxes.effective: raise ValueError('requested start precedes supported tax effective date')
    matched={}
    day_inputs={day:[] for day in calendar[:-1]}
    for action in reconcile_corporate_actions(observations,{code:code for code in selected_codes}):
        terms,sources=facts[(action.security_id,action.distribution_id)]
        credit,nominal,reserve=terms
        output_code=(query.symbols[requested_ids.index(normalize_security_id(action.security_id))]
                     if requested_ids is not None else action.event.code)
        event=replace(action.event,code=output_code)
        source_hash=canonical_hash({'observations':[source for source,_ in action.observations],'sources':sources})
        tax_hash=canonical_hash({'sources':sources,'policy':policy_hash})
        TaxLedger(taxes).register(action.distribution_id,event,nominal,reserve,tax_hash,
            credit_date=None if credit is None else date.fromisoformat(credit))
        day_inputs[event.registration_date].append(RegistrationAction(action.distribution_id,event,source_hash,
            None if credit is None else date.fromisoformat(credit),nominal,reserve,tax_hash))
    field_data={}
    if extra:
        import pyarrow as pa
        import pyarrow.parquet as pq
        refs=tuple(ref for ref in refs_under(bars_dir) if ref.name.endswith('.parquet'))
        files=tuple(source_ref(ref_name(bars_dir,ref.name))[0] for ref in refs)
        selected_paths=set(_parquet_paths_for_dates(files,panel.calendar[0],panel.calendar[-1]))
        columns=['code','date',*sorted({spec['column'] for spec in extra.values()})]
        paths=[]
        for ref in refs:
            file_name=ref_name(bars_dir, ref.name)
            path,_,_=source_ref(file_name)
            if path not in selected_paths:
                continue
            market_evidence(file_name)
            parquet=pq.ParquetFile(path)
            schema=parquet.schema_arrow
            for name,spec in extra.items():
                if spec['column'] not in schema.names:
                    raise ValueError('declared field column missing: '+name)
                kind=schema.field(spec['column']).type
                if not (pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)):
                    raise ValueError('declared field column must be numeric: '+name)
            paths.append(str(path))
        code_filter=''
        params=[paths]
        if panel.symbols:
            code_filter=' WHERE code IN (SELECT * FROM UNNEST(?))'
            params.append(list(panel.symbols))
        date_filter=' AND ' if code_filter else ' WHERE '
        date_filter+='CAST(date AS DATE) BETWEEN ? AND ?'
        params.extend((panel.calendar[0],panel.calendar[-1]))
        connection=duckdb.connect()
        try:
            table=connection.execute(
                'SELECT '+', '.join(columns)+' FROM read_parquet(?)'+code_filter+date_filter,
                params).to_arrow_table()
        finally:
            connection.close()
        codes={normalize_security_id(symbol):i for i,symbol in enumerate(panel.symbols)}
        dates={day:j for j,day in enumerate(panel.calendar)}
        field_data={name:np.full(panel.raw_close.shape,np.nan) for name in extra}
        seen=set()
        for row in table.to_pylist():
            code=normalize_security_id(str(row['code'])); raw_day=row['date']
            day=raw_day if isinstance(raw_day,date) else date.fromisoformat(raw_day)
            if code not in codes or day not in dates:
                raise ValueError('numeric field row outside selected price scope')
            identity=(code,day)
            if identity in seen: raise ValueError('duplicate numeric field security/date')
            seen.add(identity)
            i,j=codes[code],dates[day]
            for name,spec in extra.items():
                value=row[spec['column']]
                if value is None or not np.isfinite(float(value)): raise ValueError(f'numeric field missing/nonfinite: {name}, {code}, {day}')
                field_data[name][i,j]=float(value)
        for name,values in field_data.items():
            selected=list(range(len(panel.symbols)))
            if np.any(np.isfinite(panel.raw_close[selected])&~np.isfinite(values[selected])):
                raise ValueError('numeric field lacks aligned observed price rows: '+name)
    if query.symbols is not None:
        rows={normalize_security_id(symbol):index for index,symbol in enumerate(panel.symbols)}
        order=tuple(rows[identity] for identity in requested_ids)
        output_symbols=tuple(query.symbols)
        output_by_id=dict(zip(requested_ids,output_symbols))
        reorder=lambda values: values[list(order),:]
        panel=replace(panel,symbols=output_symbols,
            names=tuple(panel.names[index] for index in order),
            raw_open=reorder(panel.raw_open),raw_high=reorder(panel.raw_high),
            raw_low=reorder(panel.raw_low),raw_close=reorder(panel.raw_close),
            volume=reorder(panel.volume),amount=reorder(panel.amount),
            back_factor=reorder(panel.back_factor),traded_bar=reorder(panel.traded_bar),
            signal_close=reorder(panel.signal_close),signal_high=reorder(panel.signal_high),
            factor_events=tuple(replace(event,code=output_by_id[normalize_security_id(event.code)])
                                for event in panel.factor_events),
            listing_anchors=tuple(replace(anchor,code=output_by_id[normalize_security_id(anchor.code)])
                                  for anchor in panel.listing_anchors))
        field_data={name:reorder(values) for name,values in field_data.items()}
    if as_of is not None:
        availability=manifest.get('source_availability')
        if not isinstance(availability,list) or not availability:
            raise ValueError('point-in-time query requires unified source availability evidence')
        by_file={}
        for item in availability:
            if (not isinstance(item,dict) or set(item)!={'file','sha256','available_at'}
                    or item['file'] in by_file):
                raise ValueError('invalid unified source availability evidence')
            by_file[item['file']]=item
        for absolute,digest in hashes.items():
            name=names_by_path[absolute]
            item=by_file.get(name)
            if item is None or item['sha256']!=digest:
                raise ValueError(f'point-in-time source availability is not bound: {name}')
            try:
                available_day=date.fromisoformat(item['available_at'])
            except (TypeError,ValueError) as error:
                raise ValueError(f'invalid source availability date: {name}') from error
            if available_day>as_of:
                raise ValueError(f'source evidence became available after query cutoff: {name}')
        if not any(factor_dir in name for name in names_by_path.values()):
            raise ValueError('point-in-time query requires factor availability evidence')
    data_hash=canonical_hash({'files':market_files,'symbols':panel.symbols,'universe':scope['universe'],
        'inputs':inputs,'price_basis':'backward-factor-signal-index','fields':manifest.get('fields',{})})
    return (panel, calendar, {day: tuple(actions) for day, actions in day_inputs.items()},
            hashes, data_hash, {'policy': policy, 'source_hash': policy_hash},
            canonical_hash({'files': account_files, 'facts': [
                (identity, tuple(None if value is None else str(value) for value in terms), sources)
                for identity, (terms, sources) in sorted(facts.items())]}),
            field_data, scope, plugins)


def _prepare_raw_dataset(data, query):
    import pyarrow as pa
    import pyarrow.parquet as pq
    compare_only = query.purpose == "compare"
    required = set(query.fields) if compare_only else {"open", "high", "low", "close", "volume", "amount"}
    if query.frequency != "daily" or query.price_basis != "raw":
        raise ValueError("raw market queries require daily frequency")
    if not set(query.fields) <= required:
        raise ValueError("raw market fields must be OHLCVA")
    manifest = data.manifest(query.dataset)
    calendar_name = manifest.get("calendar")
    if not isinstance(calendar_name, str):
        raise ValueError("daily dataset calendar is required")
    calendar_payload = json.loads(data.read("sources", query.dataset, calendar_name))
    sessions, coverage = _calendar_facts(calendar_payload)
    if query.start not in sessions or query.end not in sessions:
        raise ValueError("requested interval is outside historical snapshot")
    start_index = sessions.index(query.start)
    end_index = sessions.index(query.end)
    if end_index + 1 >= len(sessions):
        raise ValueError("daily dataset must provide a following session")
    first_index = max(0, start_index - query.history + 1)
    run_days = sessions[first_index:end_index + 1]
    read_days = run_days + (sessions[end_index + 1],)
    if not _calendar_covers(coverage, run_days[0], read_days[-1]):
        raise ValueError("calendar coverage does not cover raw query warm-up, interval, and sentinel")
    needed_years = {str(day.year) for day in read_days}
    start_value = run_days[0]
    end_value = run_days[-1]
    scope = dict(manifest["scope"])
    declared = tuple(scope.get("symbols", ()))
    if not declared:
        raise ValueError("daily dataset symbols are required")
    declared_ids = {normalize_security_id(symbol) for symbol in declared}
    if query.symbols is not None:
        requested_ids = tuple(normalize_security_id(symbol) for symbol in query.symbols)
        if not set(requested_ids) <= declared_ids:
            raise ValueError("requested symbols are outside supplied dataset")
        symbols = tuple(query.symbols)
    else:
        symbols = declared
        requested_ids = tuple(normalize_security_id(symbol) for symbol in symbols)
    # Read both the requested spelling and the declared spelling.  Providers
    # may write 600036 or sh.600036 for the same canonical identity.
    read_symbols = tuple(sorted(set(declared) | set(requested_ids)))
    rows = {}
    source_hashes = {}
    for ref in manifest["files"]:
        name = ref["file"]
        parts = Path(name).parts
        if not name.endswith(".parquet") or "daily" not in parts:
            continue
        daily_index = parts.index("daily")
        identity = parts[1] if parts[0] == "source" else query.dataset
        relative = Path(*parts[daily_index:]).as_posix()
        stem = Path(parts[-1]).stem
        if stem.isdigit() and len(stem) == 4 and stem not in needed_years:
            continue
        payload = data.read("sources", identity, relative)
        source_hashes[name] = sha256(payload).hexdigest()
        parquet = pq.ParquetFile(pa.BufferReader(payload))
        names = set(parquet.schema_arrow.names)
        if not {"code", "date"} | set(query.fields) <= names:
            raise ValueError("daily Parquet fields are incomplete")
        date_type = parquet.schema_arrow.field("date").type
        date_value = start_value if pa.types.is_date(date_type) else start_value.isoformat()
        end_value_for_filter = end_value if pa.types.is_date(date_type) else end_value.isoformat()
        # ``tradestatus`` is an internal integrity input.  It is read only
        # when the published table provides it; it is never added to the
        # caller's requested fields or returned in the query result.
        columns = ["code", "date", *required, *query.fields]
        if "tradestatus" in names:
            columns.append("tradestatus")
        columns = list(dict.fromkeys(columns))
        table = pq.read_table(
            pa.BufferReader(payload), columns=columns,
            filters=[("code", "in", list(read_symbols)),
                     ("date", ">=", date_value),
                     ("date", "<=", end_value_for_filter)],
        )
        for row in table.to_pylist():
            key = (normalize_security_id(str(row.get("code"))), str(row.get("date")))
            if not key[0] or key[0] == "None":
                raise ValueError("daily row code is required")
            day = date.fromisoformat(key[1])
            value = dict(row)
            if key in rows:
                previous = rows[key]
                for field, item in value.items():
                    if field == 'code':
                        continue
                    if previous.get(field) is not None and item is not None and previous[field] != item:
                        raise ValueError(f'conflicting daily input: {key} {field}')
                    if previous.get(field) is None:
                        previous[field] = item
            else:
                rows[key] = value
    if not source_hashes:
        raise ValueError("daily dataset Parquet files are required")
    history = {symbol: deque(maxlen=query.history) for symbol in symbols}
    prepared = []
    for column, day in enumerate(run_days):
        fields, bars = {}, {}
        opening, closing = {}, {}
        for symbol, identity in zip(symbols, requested_ids):
            row = rows.get((identity, day.isoformat()), {})
            values = {field: row.get(field) for field in required}
            try:
                numbers = {
                    field: _float_or_nan(value, field, str(row.get("code")),
                                         str(day), positive=False)
                    for field, value in values.items()
                }
                if compare_only:
                    traded = False
                else:
                    prices = tuple(numbers[field] for field in
                                   ("open", "high", "low", "close"))
                    traded = _traded_bar(row.get("tradestatus"), prices,
                                         numbers["volume"], numbers["amount"])
            except _BarEvidenceError as exc:
                raise ValueError(f"{exc.reason}: {exc}") from exc
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid daily bar: {exc}") from exc
            selected = {field: values[field] for field in query.fields}
            at = datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai"))
            history_bar = HistoryBar(at, selected)
            history[symbol].append(history_bar)
            fields[symbol] = selected
            if traded and all(values[field] is not None for field in EXECUTION_FIELDS):
                bars[symbol] = Bar(symbol, day, *(float(values[field]) for field in EXECUTION_FIELDS))
            if not compare_only and all(math.isfinite(numbers[field]) for field in ("open", "close")):
                opening[symbol] = Decimal(str(values["open"]))
                closing[symbol] = Decimal(str(values["close"]))
        next_session = sessions[first_index + column + 1]
        frame = MarketFrame(
            datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai")),
            "MARKET", fields, {"next_session": next_session.isoformat()},
            {symbol: tuple(history[symbol]) for symbol in symbols},
        )
        prepared.append(HistoryDay(
            day, frame, bars, {}, opening, closing, {}, {},
            sessions[first_index + column - 1] if column else date.min,
            (), {},
        ))
    scope["symbols"] = symbols
    scope["calendar_start"] = sessions[0].isoformat()
    scope["calendar_end"] = sessions[-2].isoformat()
    return Dataset(
        query.dataset, tuple(prepared), {"kind": "raw"},
        {"source_hashes": source_hashes, "price_basis": "raw",
         "data_hash": sha256(json.dumps(source_hashes, sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()},
        scope, tuple(run_days) + (sessions[end_index + 1],), query.fields, query.history,
    )


def _prepare_dataset(data, query):
    if query.price_basis == "raw":
        return _prepare_raw_dataset(data, query)
    from core.calculation import Calculation, FeePolicy, TaxPolicy
    panel, calendar, account_inputs, hashes, data_hash, policy, account_hash, field_data, scope, plugins = _load_dataset(data, query)
    end_index = max(index for index, day in enumerate(calendar[:-1]) if day <= query.end)
    run_calendar = calendar[:end_index + 2]
    amount_liquidity = Calculation.own_day_rolling_mean(panel.amount, LIQUIDITY_WINDOW, panel.traded_bar)
    volume_liquidity = Calculation.own_day_rolling_mean(panel.volume, LIQUIDITY_WINDOW, panel.traded_bar)
    own_history = {symbol: deque(maxlen=query.history) for symbol in panel.symbols}
    session_history = {symbol: deque(maxlen=query.history) for symbol in panel.symbols}
    last_marks, last_liquidity, last_volume = {}, {}, {}
    prepared = []
    for column, day in enumerate(run_calendar[:-1]):
        at = datetime.combine(day, time(15), tzinfo=ZoneInfo("Asia/Shanghai"))
        fields, bars, opening, closing = {}, {}, {}, {}
        opening_adv, closing_adv, opening_volume20, closing_volume20 = {}, {}, {}, {}
        for row, symbol in enumerate(panel.symbols):
            values = {name: float(array[row, column]) if np.isfinite(array[row, column]) else None
                      for name, array in (
                          ("open", panel.raw_open), ("high", panel.raw_high),
                          ("low", panel.raw_low), ("close", panel.raw_close),
                          ("volume", panel.volume), ("amount", panel.amount),
                          ("signal_close", panel.signal_close))}
            values.update({name: float(array[row, column]) if np.isfinite(array[row, column]) else None
                           for name, array in field_data.items()})
            selected = {name: values[name] for name in query.fields}
            history_bar = HistoryBar(at, selected)
            session_history[symbol].append(history_bar)
            if panel.traded_bar[row, column]:
                own_history[symbol].append(history_bar)
            fields[symbol] = selected
            if any(values[name] is None for name in EXECUTION_FIELDS):
                continue
            bars[symbol] = Bar(symbol, day, *(values[name] for name in EXECUTION_FIELDS))
            opening[symbol] = Decimal(str(values["open"]))
            closing[symbol] = Decimal(str(values["close"]))
            if symbol in last_liquidity:
                opening_adv[symbol] = last_liquidity[symbol]
            if symbol in last_volume:
                opening_volume20[symbol] = last_volume[symbol]
            if np.isfinite(amount_liquidity[row, column]):
                closing_adv[symbol] = Decimal(str(amount_liquidity[row, column]))
            if np.isfinite(volume_liquidity[row, column]):
                closing_volume20[symbol] = Decimal(str(volume_liquidity[row, column]))
        frame = MarketFrame(
            at, "CN_EQUITY", fields,
            {"next_session": run_calendar[column + 1].isoformat()},
            {symbol: tuple(session_history[symbol]) for symbol in panel.symbols},
        )
        prepared.append(HistoryDay(
            day, frame, bars, dict(last_marks), opening, closing,
            opening_adv, closing_adv, calendar[column - 1] if column else date.min,
            account_inputs[day], opening_volume20,
        ))
        last_marks.update({symbol: float(value) for symbol, value in closing.items()})
        last_liquidity.update(closing_adv)
        last_volume.update(closing_volume20)
    plugin_provenance = None
    if plugins:
        prepared, plugin_provenance = _apply_signal_plugins(prepared, plugins, query.start, query.end)
    provenance = {
        "source_hashes": hashes, "data_hash": data_hash,
        "account_hash": account_hash, "state_start": calendar[0].isoformat(),
        "as_of": None if query.as_of is None else query.as_of.isoformat(),
        "query_semantics": "point_in_time" if query.as_of is not None else "snapshot_replay",
    }
    if plugin_provenance is not None:
        provenance["signal_plugins"] = plugin_provenance
    return Dataset(
        query.dataset, tuple(prepared),
        {"fees": FeePolicy.from_mapping(policy["policy"]["fees"], policy["source_hash"]),
         "tax_policy": TaxPolicy.from_mapping(policy["policy"]["tax"], policy["source_hash"])},
        provenance, {**scope, "symbols": tuple(panel.symbols)},
        tuple(run_calendar), query.fields, query.history,
    )


def _apply_signal_plugins(records, plugins, start, end):
    from dataclasses import replace
    from protocol import HistoryBar, MarketFrame
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
        merged = merge(records, transformed, selected_fields)
        provenance['adapter'] = {
            'module': module_name, 'method': adapter_spec['method'],
            'fields': selected_fields,
        }
        records = merged

    scaler_spec = plugins.get('scaler')
    if scaler_spec is not None:
        if (not isinstance(scaler_spec, dict)
                or set(scaler_spec) - {'module', 'fields', 'fit_start', 'fit_end', 'params'}
                or not scaler_spec.get('module') or not scaler_spec.get('fields')
                or not isinstance(scaler_spec.get('fit_start'), str)
                or not isinstance(scaler_spec.get('fit_end'), str)):
            raise ValueError('signal scaler requires module, fields, fit_start, fit_end, and params')
        try:
            fit_start = date.fromisoformat(scaler_spec['fit_start'])
            fit_end = date.fromisoformat(scaler_spec['fit_end'])
        except ValueError as error:
            raise ValueError('signal scaler fit dates are invalid') from error
        if fit_end >= start:
            raise ValueError('signal scaler fit end before backtest start is required')
        available = tuple(record.trade_date for record in records)
        if fit_start not in available or fit_end not in available:
            raise ValueError('signal scaler fit window is not fully covered')
        module_name = scaler_spec['module']
        if not isinstance(module_name, str) or ':' not in module_name:
            raise ValueError('signal scaler module must be explicit')
        module, name = module_name.split(':', 1)
        scaler_type = getattr(import_module(module), name)
        selected_fields = names(scaler_spec['fields'], 'signal scaler fields')
        training = []
        for record in records:
            if fit_start <= record.trade_date <= fit_end:
                for values in record.frame.fields.values():
                    training.append([values[field] for field in selected_fields])
        if not training:
            raise ValueError('signal scaler fit window is not fully covered')
        scaler = scaler_type(**dict(scaler_spec.get('params', {})))
        try:
            scaler.fit(training)
            statistics = scaler.statistics()
            transformed = []
            for record in records:
                frames = projection(record, selected_fields)
                values = []
                locations = []
                for symbol, fields in frames.fields.items():
                    values.append([fields[field] for field in selected_fields])
                    locations.append((symbol, fields))
                for symbol, rows in frames.history.items():
                    for row in rows:
                        values.append([row.fields[field] for field in selected_fields])
                        locations.append((symbol, row.fields))
                output = iter(scaler.transform(values))
                fields = {}
                for symbol, original in locations[:len(frames.fields)]:
                    result = next(output)
                    fields[symbol] = {field: float(result[index]) for index, field in enumerate(selected_fields)}
                history = {}
                offset = len(frames.fields)
                for symbol, rows in frames.history.items():
                    history[symbol] = []
                    for row in rows:
                        result = next(output)
                        history[symbol].append(HistoryBar(
                            row.at, {field: float(result[index]) for index, field in enumerate(selected_fields)}))
                    history[symbol] = tuple(history[symbol])
                transformed.append(replace(frames, fields=fields, history=history))
            transformed = Adapter.validate(tuple(transformed), fields=selected_fields)
        except Exception as error:
            raise ValueError(f'signal scaler transform: {error}') from error
        records = merge(records, transformed, selected_fields)
        serial_statistics = {
            key: value.tolist() if hasattr(value, 'tolist') else value
            for key, value in statistics.items()
        }
        provenance['scaler'] = {
            'module': module_name, 'fields': selected_fields,
            'fit_start': fit_start.isoformat(), 'fit_end': fit_end.isoformat(),
            'statistics': serial_statistics,
            'statistics_hash': canonical_hash(serial_statistics),
        }
    return tuple(records), provenance
