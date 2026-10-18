"""Data plugins used by the kernel boundary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from bisect import bisect_right
from numbers import Real
from types import MappingProxyType
from zoneinfo import ZoneInfo

import numpy as np
from core.calculation import AllocationLimits
from protocol import HistoryResult, RegistrationRecord


_MARKET_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "between"}


def _known_at(value):
    if not isinstance(value, str):
        raise ValueError("market known_at must be an explicit timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("market known_at must include date and time") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("market known_at must include a timezone")
    return result


def _effective(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            if len(value) == 10:
                return date.fromisoformat(value)
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed.date()
        except ValueError:
            pass
    raise ValueError("market effective date is invalid")


def _market_value(value, kind):
    if kind == "number":
        if type(value) not in (int, float, Decimal) or not np.isfinite(float(value)):
            raise ValueError("market filter requires a finite number")
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("market filter requires a string")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("market filter requires a boolean")
    elif kind == "date":
        _effective(value)
    else:
        raise ValueError("unsupported market field type")
    return value


def _matches(actual, condition, kind):
    op, expected = condition["op"], condition["value"]
    actual = _market_value(actual, kind)
    if op == "in":
        values = tuple(_market_value(value, kind) for value in expected)
        return actual in values
    if op == "between":
        low, high = (_market_value(value, kind) for value in expected)
        if low > high:
            raise ValueError("market between bounds must be ordered")
        return low <= actual <= high
    expected = _market_value(expected, kind)
    return {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
    }[op]()


def _resolve_eligible(market, registry, days, symbols, daily_values,
                      membership_rows, fact_rows=None):
    """Resolve daily close and opening eligibility using only known evidence."""
    from protocol import _market_selection

    selection = _market_selection(market)
    if selection["scope"] != registry.get("scope"):
        raise ValueError("market scope is not registered")
    fields = registry.get("fields")
    members = registry.get("members")
    if not isinstance(fields, Mapping):
        raise ValueError("market fields are not registered")
    if not isinstance(members, Mapping):
        members = {}
    timezone_name = registry.get("timezone")
    try:
        zone = ZoneInfo(timezone_name)
        opening_time = time.fromisoformat(registry["session_open"])
        closing_time = time.fromisoformat(registry["session_close"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("market session calendar readiness is missing") from error
    days = tuple(days)
    symbols = tuple(symbols)
    if not days or not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("market resolution requires ordered days and unique symbols")
    code_column = members.get("code")
    index_column = members.get("index")
    effective_column = members.get("effective")
    known_column = members.get("known")
    included_column = members.get("included")
    codes = selection.get("codes")
    if codes is not None and not set(codes) <= set(symbols):
        raise ValueError("market codes are outside available source data")
    index = selection.get("index")
    if (index is not None or codes is None) and not (code_column and effective_column
            and known_column and included_column):
        raise ValueError("market membership data is missing")
    if index is not None and not index_column:
        raise ValueError("index membership data is missing")
    membership_rows = tuple(membership_rows or ())
    if (index is not None and not any(row.get(index_column) == index for row in membership_rows)
            or codes is None and index is None and not any(
                row.get(index_column) in (None, "") for row in membership_rows)):
        raise ValueError("market membership data is missing")
    fact_rows = fact_rows or {}
    daily_index = {}
    for field_name, spec in fields.items():
        if spec.get("source") != "daily":
            continue
        column = spec.get("column")
        for observed_day, rows in daily_values.items():
            for code, values in rows.items():
                cell = values.get(column)
                if cell is not None:
                    if not isinstance(cell, Mapping) or "value" not in cell or "known_at" not in cell:
                        raise ValueError(f"market daily field needs value and known_at: {field_name}")
                    daily_index.setdefault((field_name, code), []).append(
                        (observed_day, _known_at(cell["known_at"]), cell["value"]))
    daily_dates = {}
    for key, records in daily_index.items():
        records.sort(key=lambda item: item[0])
        daily_dates[key] = tuple(item[0] for item in records)
    fact_index = {}
    for field_name, spec in fields.items():
        if spec.get("source") != "facts":
            continue
        for row in fact_rows.get(field_name, ()):
            code = row.get(spec.get("code"))
            fact_index.setdefault((field_name, code), []).append(
                (_effective(row.get(spec.get("effective"))),
                 _known_at(row.get(spec.get("known"))), row.get(spec.get("column"))))
    fact_effective = {}
    for key, records in fact_index.items():
        records.sort(key=lambda item: (item[0], item[1]))
        fact_effective[key] = tuple(item[0] for item in records)
    conditions = selection.get("filters", ())
    for condition in conditions:
        spec = fields.get(condition["field"])
        if not isinstance(spec, Mapping) or spec.get("source") not in {"daily", "facts"}:
            raise ValueError(f"market filter field is not registered: {condition['field']}")
        if condition["op"] not in _MARKET_OPS:
            raise ValueError("unsupported market filter operator")
        kind = spec.get("type")
        expected = condition["value"]
        if condition["op"] in {"in", "between"}:
            values = tuple(_market_value(value, kind) for value in expected)
            if condition["op"] == "between":
                low, high = values
                if kind == "date":
                    low, high = _effective(low), _effective(high)
                if low > high:
                    raise ValueError("market between bounds must be ordered")
        else:
            _market_value(expected, kind)
        if spec["source"] == "facts" and not all(spec.get(key) for key in
                ("code", "column", "effective", "known")):
            raise ValueError("market fact field needs code, value, effective and known columns")

    def active_members(day, cutoff, selected_index):
        grouped = {}
        for row in membership_rows:
            if selected_index is None:
                if index_column and row.get(index_column) not in (None, ""):
                    continue
            elif row.get(index_column) != selected_index:
                continue
            code = row.get(code_column)
            effective = _effective(row.get(effective_column))
            known = _known_at(row.get(known_column))
            if code not in symbols or effective > day or known > cutoff:
                continue
            prior = grouped.get(code)
            if prior is None or (effective, known) > prior[:2]:
                included = row.get(included_column)
                if type(included) is not bool:
                    raise ValueError("market membership included value must be boolean")
                grouped[code] = (effective, known, included)
        return {code for code, state in grouped.items() if state[2]}

    def fact_value(field, code, day, cutoff):
        spec = fields[field]
        if spec["source"] == "daily":
            key = (field, code)
            records = daily_index.get(key, ())
            position = bisect_right(daily_dates.get(key, ()), day) - 1
            while position >= 0:
                observed, known, value = records[position]
                if known <= cutoff:
                    return value
                position -= 1
            return None
        key = (field, code)
        records = fact_index.get(key, ())
        position = bisect_right(fact_effective.get(key, ()), day) - 1
        best = None
        while position >= 0:
            item = records[position]
            if item[1] <= cutoff and (best is None or item[:2] > best[:2]):
                best = item
            position -= 1
        return None if best is None else best[2]

    def for_time(day, cutoff):
        if codes is not None:
            candidates = set(codes)
            if index is not None:
                candidates &= active_members(day, cutoff, index)
            elif code_column and effective_column and known_column and included_column:
                scope_members = active_members(day, cutoff, None)
                candidates &= scope_members
        else:
            candidates = active_members(day, cutoff, index)
        result = []
        ordered = codes if codes is not None else symbols
        for code in ordered:
            if code not in candidates:
                continue
            if all((value := fact_value(condition["field"], code, day, cutoff)) is not None
                   and _matches(value, condition, fields[condition["field"]]["type"])
                   for condition in conditions):
                result.append(code)
        return tuple(result)

    closing, opening = {}, {}
    for day in days:
        close_at = datetime.combine(day, closing_time, zone)
        open_at = datetime.combine(day, opening_time, zone)
        closing[day] = for_time(day, close_at)
        opening[day] = for_time(day, open_at)
    return closing, opening


def _prepare_market(data, query, dataset):
    """Apply a registered market selector to daily strategy inputs."""
    import json
    from dataclasses import replace
    from hashlib import sha256
    from protocol import DataError, normalize_security_id

    registry = json.loads(data.read("sources", "registry", "sources.json"))
    entries = registry.get("markets")
    if not isinstance(entries, list):
        raise DataError("market_not_registered", "market registry is missing")
    matches = [item for item in entries if isinstance(item, Mapping)
               and item.get("scope") == query.market["scope"]]
    if len(matches) != 1:
        raise DataError("market_not_registered", "market scope must have one registry entry")
    entry = matches[0]
    manifest = data.manifest(query.dataset)
    if (entry.get("dataset_id") != query.dataset
            or entry.get("calendar") != manifest.get("calendar")
            or not isinstance(entry.get("currency"), str) or not entry["currency"].strip()
            or not isinstance(entry.get("execution"), str) or not entry["execution"].strip()):
        raise DataError("market_not_ready", "market dataset, currency, calendar or execution is not ready")
    if entry.get("code_normalizer") not in (None, "normalize_security_id"):
        raise DataError("market_not_ready", "market code normalizer is not registered")
    normalize = normalize_security_id if entry.get("code_normalizer") else lambda value: value

    native_symbols = tuple(dataset.scope.get("symbols", ()))
    if not native_symbols:
        raise DataError("market_data_missing", "market source has no declared securities")
    symbol_map = {}
    for symbol in native_symbols:
        identity = normalize(symbol)
        if identity in symbol_map:
            raise DataError("market_data_invalid", "market source has duplicate security identities")
        symbol_map[identity] = symbol
    days = tuple(record.trade_date for record in dataset.records)
    used_fields = {condition["field"] for condition in query.market.get("filters", ())}
    fields = entry.get("fields", {})
    daily = {}
    for record in dataset.records:
        daily[record.trade_date] = {
            normalize(symbol): {
                spec["column"]: {"value": values.get(spec["column"]),
                                 "known_at": datetime.combine(
                                     record.trade_date,
                                     time.fromisoformat(entry["session_close"]),
                                     ZoneInfo(entry["timezone"]),
                                 ).isoformat()}
                for name, spec in fields.items() if name in used_fields
                and isinstance(spec, Mapping) and spec.get("source") == "daily"
            }
            for symbol, values in record.frame.fields.items()
        }
    source_hashes = {}

    def read_rows(binding, label):
        if binding is None:
            return ()
        if not isinstance(binding, Mapping) or not isinstance(binding.get("source"), str) \
                or not isinstance(binding.get("file"), str):
            raise DataError("market_data_invalid", f"market {label} source binding is invalid")
        payload = data.read("sources", binding["source"], binding["file"])
        source_hashes[binding["source"] + "/" + binding["file"]] = sha256(payload).hexdigest()
        value = json.loads(payload)
        rows = value.get("records") if isinstance(value, Mapping) else value
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise DataError("market_data_invalid", f"market {label} rows are invalid")
        return tuple(dict(row) for row in rows)

    member_binding = entry.get("members")
    membership = read_rows(member_binding, "membership")
    if membership and entry.get("code_normalizer"):
        for row in membership:
            if member_binding.get("code") not in row:
                raise DataError("market_data_invalid", "market membership code column is missing")
            row[member_binding["code"]] = normalize(str(row[member_binding["code"]]))
    facts = {}
    for field_name, spec in fields.items():
        if field_name not in used_fields:
            continue
        if isinstance(spec, Mapping) and spec.get("source") == "facts":
            facts[field_name] = read_rows(spec.get("binding"), field_name)
            if entry.get("code_normalizer"):
                for row in facts[field_name]:
                    row[spec["code"]] = normalize(str(row[spec["code"]]))
    try:
        selection = dict(query.market)
        if selection.get("codes") is not None:
            selection["codes"] = tuple(normalize(code) for code in selection["codes"])
        selected, opening = Adapter.eligible(
            selection, {**entry, "members": member_binding}, days,
            tuple(symbol_map), daily, membership, facts,
        )
        normalized_codes = query.market.get("codes")
        if normalized_codes is not None:
            requested = tuple(normalize(code) for code in normalized_codes)
            if any(code not in symbol_map for code in requested):
                raise ValueError("market codes are outside available source data")
        if query.market.get("index") is not None and not membership:
            raise ValueError("index membership data is missing")
    except (KeyError, TypeError, ValueError) as error:
        raise DataError("market_data_missing", str(error)) from error

    records = []
    counts = {}
    for record in dataset.records:
        current = tuple(symbol_map[code] for code in selected[record.trade_date])
        opening_symbols = tuple(symbol_map[code] for code in opening[record.trade_date])
        frame = replace(
            record.frame,
            fields={symbol: record.frame.fields[symbol] for symbol in current},
            history={symbol: record.frame.history[symbol] for symbol in current},
        )
        records.append(replace(record, frame=frame, opening_eligible=opening_symbols))
        counts[record.trade_date.isoformat()] = len(current)
    resolved = {
        "selection": {key: list(value) if isinstance(value, tuple) else
                      [dict(item) for item in value] if key == "filters" else value
                      for key, value in query.market.items()},
        "currency": entry["currency"], "calendar": entry["calendar"],
        "execution": entry["execution"], "eligible_counts": counts,
        "source_hashes": source_hashes,
    }
    return replace(dataset, records=tuple(records),
                   scope={**dict(dataset.scope), "market": resolved},
                   provenance={**dict(dataset.provenance), "market": resolved})


class Scaler:
    """Fit column statistics on rows and reuse them for later rows."""

    def __init__(
        self,
        method: str = "standard",
        *,
        feature_range: tuple[float, float] = (0.0, 1.0),
        quantile_range: tuple[float, float] = (25.0, 75.0),
    ) -> None:
        if method not in {"standard", "minmax", "robust"}:
            raise ValueError("method must be standard, minmax, or robust")
        self.method = method
        self.feature_range = self._validate_range(feature_range, "feature_range")
        self.quantile_range = self._validate_range(quantile_range, "quantile_range")
        if not 0 <= self.quantile_range[0] < self.quantile_range[1] <= 100:
            raise ValueError("quantile_range must be increasing within 0 and 100")
        self.center_ = None
        self.scale_ = None
        self.n_features_in_ = None

    @staticmethod
    def _validate_range(value: tuple[float, float], name: str) -> tuple[float, float]:
        if len(value) != 2:
            raise ValueError(f"{name} must contain two values")
        low, high = (float(item) for item in value)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                width = high - low
        except FloatingPointError as error:
            raise ValueError(f"{name} width must be finite") from error
        if not np.isfinite([low, high, width]).all() or low >= high:
            raise ValueError(f"{name} must be finite and increasing")
        return low, high

    @staticmethod
    def _matrix(values) -> np.ndarray:
        try:
            raw = np.asarray(values, dtype=object)
        except (TypeError, ValueError) as error:
            raise ValueError("values must be a two-dimensional numeric matrix") from error
        if raw.ndim != 2 or raw.shape[0] == 0 or raw.shape[1] == 0:
            raise ValueError("values must be a non-empty two-dimensional matrix")
        if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) for value in raw.flat):
            raise ValueError("values must contain only real numbers")
        try:
            matrix = raw.astype(float, copy=False)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("values must contain only real numbers") from error
        if not np.isfinite(matrix).all():
            raise ValueError("values must contain only finite numbers")
        return matrix

    def fit(self, values) -> "Scaler":
        matrix = self._matrix(values)
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                if self.method == "standard":
                    center = matrix.mean(axis=0)
                    scale = matrix.std(axis=0, ddof=0)
                elif self.method == "minmax":
                    center = matrix.min(axis=0)
                    scale = matrix.max(axis=0) - center
                else:
                    low, high = self.quantile_range
                    center = np.median(matrix, axis=0)
                    scale = np.percentile(matrix, high, axis=0) - np.percentile(matrix, low, axis=0)
                if not np.isfinite(center).all() or not np.isfinite(scale).all():
                    raise ValueError("fit statistics are not finite")
                scale = np.where(scale == 0, 1.0, scale)
        except FloatingPointError as error:
            raise ValueError("fit statistics are not finite") from error
        self.center_ = center
        self.scale_ = scale
        self.n_features_in_ = matrix.shape[1]
        return self

    def transform(self, values) -> np.ndarray:
        if self.center_ is None or self.scale_ is None:
            raise ValueError("scaler is not fitted")
        matrix = self._matrix(values)
        if matrix.shape[1] != self.n_features_in_:
            raise ValueError("values have a different number of features")
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                result = (matrix - self.center_) / self.scale_
                if self.method == "minmax":
                    low, high = self.feature_range
                    result = result * (high - low) + low
                if not np.isfinite(result).all():
                    raise ValueError("transform result is not finite")
        except FloatingPointError as error:
            raise ValueError("transform result is not finite") from error
        return result

    def statistics(self):
        if self.center_ is None or self.scale_ is None:
            raise ValueError("scaler is not fitted")
        return {"center": self.center_.copy(), "scale": self.scale_.copy()}


class Adapter:
    """Pure validation and calendar/basis adaptation for normalized market frames.

    ``adjust`` requires each factor to carry explicit ``factor``, ``direction``,
    ``anchor``, ``as_of`` and ``split_only`` evidence. Prices are multiplied
    and volume is divided only by a proven split-only factor. Cash dividends
    and supplier total-adjustment factors are never used for volume.
    """

    @classmethod
    def candidates(cls, data, market, dataset_id, start, end):
        import json
        from protocol import normalize_security_id
        registry = json.loads(data.read("sources", "registry", "sources.json"))
        entries = registry.get("markets")
        matches = [item for item in entries or () if isinstance(item, Mapping)
                   and item.get("scope") == market["scope"]]
        if len(matches) != 1 or matches[0].get("dataset_id") != dataset_id:
            raise ValueError("market scope is not registered for this dataset")
        entry = matches[0]
        binding = entry.get("members")
        codes = market.get("codes")
        index = market.get("index")
        normalize = (normalize_security_id if entry.get("code_normalizer")
                     == "normalize_security_id" else lambda value: value)
        if codes is not None and index is None and binding is None:
            return tuple(codes)
        if not isinstance(binding, Mapping):
            raise ValueError("market membership data is missing")
        payload = data.read("sources", binding["source"], binding["file"])
        rows = json.loads(payload)
        rows = rows.get("records") if isinstance(rows, Mapping) else rows
        if not isinstance(rows, list):
            raise ValueError("market membership rows are invalid")
        code_key, index_key = binding.get("code"), binding.get("index")
        scope_codes, index_codes = [], []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get(code_key), str):
                raise ValueError("market membership code is invalid")
            if row.get(binding.get("included")) is not True:
                continue
            code = row[code_key]
            try:
                if _effective(row.get(binding.get("effective"))) > end:
                    continue
                if _known_at(row.get(binding.get("known"))).date() > end:
                    continue
            except (TypeError, ValueError):
                raise ValueError("market membership effective/known dates are invalid")
            if row.get(index_key) in (None, ""):
                if normalize(code) not in {normalize(item) for item in scope_codes}:
                    scope_codes.append(code)
            if (index is not None and row.get(index_key) == index
                    and normalize(code) not in {normalize(item) for item in index_codes}):
                index_codes.append(code)
        if codes is not None:
            base = list(codes)
            allowed = set(index_codes if index is not None else scope_codes)
            allowed_by_id = {normalize(code): code for code in allowed}
            return tuple(allowed_by_id[normalize(code)] for code in base
                         if normalize(code) in allowed_by_id)
        return tuple(index_codes if index is not None else scope_codes)

    @classmethod
    def eligible(cls, market, registry, days, symbols, daily_values,
                 membership_rows, fact_rows=None):
        return _resolve_eligible(market, registry, days, symbols, daily_values,
                                 membership_rows, fact_rows)

    @classmethod
    def market(cls, data, query, dataset):
        return _prepare_market(data, query, dataset)

    @classmethod
    def validate(cls, frames, *, symbols=None, fields=None, missing="reject"):
        from protocol import MarketFrame

        frames = tuple(frames)
        if not frames or any(not isinstance(frame, MarketFrame) for frame in frames):
            raise ValueError("frames must be a non-empty MarketFrame sequence")
        if missing not in {"reject", "allow"}:
            raise ValueError("missing must be reject or allow")
        expected_symbols = tuple(frames[0].fields) if symbols is None else tuple(symbols)
        if not expected_symbols or len(set(expected_symbols)) != len(expected_symbols):
            raise ValueError("symbols must be an ordered unique nonempty tuple")
        required_fields = None if fields is None else tuple(fields)
        if required_fields is not None and len(set(required_fields)) != len(required_fields):
            raise ValueError("fields must be unique")

        def check_values(values, expected, label, *, exact):
            actual = tuple(values)
            if exact and actual != tuple(expected):
                raise ValueError(f"{label} fields do not match required fields")
            for field, value in values.items():
                if isinstance(value, (bool, np.bool_)):
                    raise ValueError(f"boolean market value: {label}.{field}")
                try:
                    finite = np.isfinite(float(value))
                except (TypeError, ValueError, OverflowError):
                    finite = False
                if not finite:
                    raise ValueError(f"non-finite market value: {label}.{field}")
            if missing == "reject" and any(field not in values for field in expected):
                raise ValueError(f"{label} is missing a required field")

        previous = None
        for frame in frames:
            if previous is not None and frame.at <= previous:
                raise ValueError("frames must be ordered by unique timestamp")
            previous = frame.at
            actual_symbols = tuple(frame.fields)
            if missing == "reject" and actual_symbols != expected_symbols:
                raise ValueError("frame symbol identity/order changed")
            if missing == "allow":
                if any(symbol not in expected_symbols for symbol in actual_symbols):
                    raise ValueError("frame contains an undeclared symbol")
                if tuple(symbol for symbol in expected_symbols if symbol in actual_symbols) != actual_symbols:
                    raise ValueError("frame symbol order changed")
            for symbol in actual_symbols:
                values = frame.fields[symbol]
                check_fields = tuple(values) if required_fields is None else required_fields
                check_values(values, check_fields, symbol,
                             exact=missing == "reject" or required_fields is None)
                rows = frame.history.get(symbol, ())
                previous_row = None
                for row in rows:
                    if previous_row is not None and row.at <= previous_row:
                        raise ValueError(f"history timestamps must be strictly increasing: {symbol}")
                    if row.at > frame.at:
                        raise ValueError(f"future history row: {symbol}")
                    previous_row = row.at
                    row_fields = check_fields if required_fields is not None else tuple(values)
                    check_values(row.fields, row_fields, f"{symbol} history",
                                 exact=missing == "reject" or required_fields is None)
            extra_history = set(frame.history) - set(actual_symbols)
            if extra_history:
                raise ValueError("history contains an undeclared symbol")
        return frames

    @staticmethod
    def _factor(value, symbol, anchor, as_of, timestamp):
        from datetime import date, datetime, time

        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(f"adjustment timestamp must be timezone-aware: {symbol}")

        def stamp(item, timezone):
            if isinstance(item, datetime):
                if item.tzinfo is None or item.utcoffset() is None:
                    raise ValueError("adjustment datetime must be timezone-aware")
                return item
            if isinstance(item, date):
                return datetime.combine(item, time.min, tzinfo=timezone)
            raise ValueError("adjustment dates must be date or datetime")

        if (not isinstance(value, Mapping) or value.get("split_only") is not True
                or value.get("direction") != "raw_to_target"
                or value.get("anchor") != anchor or "as_of" not in value
                or "effective_at" not in value or "known_at" not in value
                or "factor" not in value):
            raise ValueError(f"explicit split-only adjustment evidence required: {symbol}")
        try:
            timezone = timestamp.tzinfo
            effective_at = stamp(value["effective_at"], timezone)
            known_at = stamp(value["known_at"], timezone)
            point_at = stamp(timestamp, timezone)
            cutoff = stamp(as_of, timezone)
            factor_as_of = stamp(value["as_of"], timezone)
        except ValueError as error:
            raise ValueError(f"invalid adjustment dates: {symbol}") from error
        if factor_as_of != cutoff:
            raise ValueError(f"adjustment as_of mismatch: {symbol}")
        if effective_at > point_at or known_at > point_at or known_at > cutoff:
            raise ValueError(f"future adjustment evidence: {symbol}")
        try:
            factor = float(value["factor"])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"invalid adjustment factor: {symbol}") from error
        if not np.isfinite(factor) or factor <= 0:
            raise ValueError(f"invalid adjustment factor: {symbol}")
        return factor

    @classmethod
    def align(cls, frames, calendar, *, symbols=None, fields=None, missing="reject"):
        frames = cls.validate(frames, symbols=symbols, fields=fields, missing=missing)
        calendar = tuple(calendar)
        if not calendar or tuple(sorted(set(calendar))) != calendar:
            raise ValueError("calendar must be ordered and unique")
        by_time = {frame.at: frame for frame in frames}
        absent = [at for at in calendar if at not in by_time]
        if absent and missing == "reject":
            raise ValueError(f"calendar observations missing: {absent[0]}")
        if missing == "allow":
            return tuple(by_time.get(at) for at in calendar)
        return tuple(by_time[at] for at in calendar)

    @classmethod
    def adjust(cls, frames, *, basis, as_of, factors, anchor="back", volume_field="volume"):
        from dataclasses import replace
        from datetime import date
        from protocol import HistoryBar

        if basis == "adjusted":
            raise ValueError("total adjusted basis is not implemented")
        if basis not in {"raw", "split-adjusted"}:
            raise ValueError("unsupported price basis")
        if anchor not in {"fore", "back"}:
            raise ValueError("anchor must be fore or back")
        if not isinstance(as_of, date) or not hasattr(factors, "items"):
            raise ValueError("explicit as_of and factors are required")
        frames = cls.validate(frames, missing="allow")
        from datetime import datetime, time

        def cutoff_for(frame):
            if isinstance(as_of, datetime):
                if as_of.tzinfo is None or as_of.utcoffset() is None:
                    raise ValueError("as_of must be timezone-aware")
                return as_of
            return datetime.combine(as_of, time.min, tzinfo=frame.at.tzinfo)

        for frame in frames:
            if frame.context.get("price_basis") != "raw":
                raise ValueError("adjust requires an explicit raw price_basis")
            cutoff = cutoff_for(frame)
            if frame.at > cutoff or any(row.at > cutoff for rows in frame.history.values() for row in rows):
                raise ValueError("frame or history is later than as_of")
        if basis == "raw":
            return tuple(
                replace(
                    frame,
                    context={
                        **frame.context,
                        "price_basis": basis,
                        "as_of": as_of.isoformat(),
                        "adjustment_anchor": anchor,
                        "adjustment_direction": "raw_to_target",
                    },
                )
                for frame in frames
            )
        output = []
        for frame in frames:
            fields = {}
            for symbol, values in frame.fields.items():
                if (symbol, frame.at) not in factors:
                    raise ValueError(f"missing adjustment factor: {symbol}")
                factor = cls._factor(factors[(symbol, frame.at)], symbol, anchor, as_of, frame.at)
                try:
                    with np.errstate(over="raise", invalid="raise", divide="raise"):
                        adjusted = dict(values)
                        for field, value in values.items():
                            if value is None:
                                continue
                            if field == volume_field:
                                adjusted[field] = float(value) / factor
                            elif field in {"open", "high", "low", "close", "price"}:
                                adjusted[field] = float(value) * factor
                            if field in {volume_field, "open", "high", "low", "close", "price"} and not np.isfinite(adjusted[field]):
                                raise ValueError(f"adjusted value is not finite: {symbol}")
                except FloatingPointError as error:
                    raise ValueError(f"adjusted value is not finite: {symbol}") from error
                fields[symbol] = adjusted
            history = {}
            for symbol, rows in frame.history.items():
                adjusted_rows = []
                for row in rows:
                    if (symbol, row.at) not in factors:
                        raise ValueError(f"missing adjustment factor: {symbol}")
                    factor = cls._factor(factors[(symbol, row.at)], symbol, anchor, as_of, row.at)
                    try:
                        with np.errstate(over="raise", invalid="raise", divide="raise"):
                            values = dict(row.fields)
                            for field, value in row.fields.items():
                                if value is None:
                                    continue
                                if field == volume_field:
                                    values[field] = float(value) / factor
                                elif field in {"open", "high", "low", "close", "price"}:
                                    values[field] = float(value) * factor
                                if field in {volume_field, "open", "high", "low", "close", "price"} and not np.isfinite(values[field]):
                                    raise ValueError(f"adjusted history value is not finite: {symbol}")
                    except FloatingPointError as error:
                        raise ValueError(f"adjusted history value is not finite: {symbol}") from error
                    adjusted_rows.append(HistoryBar(row.at, values))
                history[symbol] = tuple(adjusted_rows)
            context = {
                **frame.context,
                "price_basis": basis,
                "as_of": as_of.isoformat(),
                "adjustment_anchor": anchor,
                "adjustment_direction": "raw_to_target",
            }
            output.append(replace(frame, fields=fields, context=context, history=history))
        return tuple(output)


class Settlement:
    """Stateful account settlement extracted from the historical runner."""

    DEFAULT_EXECUTION = MappingProxyType({
        "price": MappingProxyType({"model": "ratio", "params": MappingProxyType({"rate": "0.001", "tick": "0.01"})}),
        "quantity": MappingProxyType({"model": "capacity", "params": MappingProxyType({})}),
        "fees": MappingProxyType({"model": "dated", "params": MappingProxyType({"rounding": "none"})}),
        "tax": MappingProxyType({"model": "fifo", "params": MappingProxyType({})}),
        "check": MappingProxyType({"model": "cn_a_share", "params": MappingProxyType({"tick": "0.01", "min_qty": 100, "step": 100})}),
    })

    @classmethod
    def _copy(cls, value):
        return {key: cls._copy(item) if isinstance(item, Mapping) else item
                for key, item in value.items()} if isinstance(value, Mapping) else value

    @classmethod
    def resolve(cls, *, limits=None, execution=None):
        from core.calculation import AllocationLimits
        from protocol import _limits, validate_execution

        if execution is None:
            execution = cls._copy(cls.DEFAULT_EXECUTION)
        execution = validate_execution(execution)
        step = execution["check"]["params"]["step"]
        if limits is None:
            limits = AllocationLimits(lot_size=step)
        elif isinstance(limits, dict):
            limits = _limits(limits, lot_size=step)
        elif isinstance(limits, AllocationLimits):
            limits = AllocationLimits(
                lot_size=step,
                stock_weight=limits.stock_weight,
                adv_fraction=limits.adv_fraction,
            )
        else:
            raise TypeError("limits must be AllocationLimits or a mapping")
        return limits, execution

    @classmethod
    def describe(cls, overrides):
        limits, execution = cls.resolve(**dict(overrides))
        return {
            "module": f"{cls.__module__}:{cls.__name__}",
            "params": {
                "limits": {"lot_size": limits.lot_size,
                            "stock_weight": limits.stock_weight,
                            "adv_fraction": limits.adv_fraction},
                "execution": execution,
            },
        }

    def __init__(self, initial_cash, fees, tax_policy, *, limits=None, execution=None):
        from core.calculation import (AccountExecutionSession, AccountLedger,
                                      AccountDistributionLedger, AllocationLimits, TradeRules)

        limits, execution = self.resolve(limits=limits, execution=execution)

        self.limits = limits
        self.trade_rules = TradeRules(execution, fees, tax_policy)
        self.account = AccountLedger(initial_cash)
        self.execution = AccountExecutionSession(self.account, self.trade_rules)
        self.distributions = AccountDistributionLedger(self.account)
        self.taxes = self.trade_rules._tax_ledger
        self.tax_liabilities = []
        self.frames = []
        self.targets = []
        self.allocations = []
        self.points = []
        self.holdings = []
        self.postings = []
        self.registrations = []
        self._pending = None
        self._last_day = None
        self._following = None
        self._failed = False
        self._failure = None
        self._sealed = False

    @staticmethod
    def validate_dataset(dataset, request, *, limits=None, execution=None):
        from core.calculation import TradeRules

        limits, execution = Settlement.resolve(limits=limits, execution=execution)
        market = dataset.scope.get("market")
        if isinstance(market, Mapping) and market.get("execution") != execution["check"]["model"]:
            raise ValueError("market execution readiness does not match settlement")
        rules = TradeRules(execution, dataset.policies['fees'], dataset.policies['tax_policy'])
        return rules.check(phase='dataset', dataset=dataset, start=request.start.date())

    @property
    def cash(self):
        return self.account.cash

    @property
    def lots(self):
        return self.account.lots

    def apply(self, record, following, trade_start=None):
        self._ensure_active()
        try:
            return self._apply(record, following, trade_start)
        except Exception as error:
            self.fail(error)
            raise

    def _ensure_active(self):
        if self._failed:
            raise ValueError("settlement has failed; partial result is evidence only")

    def fail(self, error=None):
        self.seal(error)

    def seal(self, error=None):
        self._failed = True
        self._failure = error
        self._sealed = True

    @staticmethod
    def _validate_day(record, day):
        import re
        if record.trade_date != day or record.frame.at.date() != day:
            raise ValueError('history day/frame do not match required calendar session')
        if record.liquidity_as_of >= day:
            raise ValueError('opening liquidity cannot use current/future session')
        for action in record.registrations:
            event = action.event
            if (not action.distribution_id or not event.executable
                    or event.registration_date != day
                    or not re.fullmatch('[0-9a-f]{64}', action.source_sha256)):
                raise ValueError('identified executable registration action required')
            if event.stock_dividend_per_share + event.reserve_to_stock_per_share > 0:
                if (not isinstance(action.share_credit_date, date)
                        or not event.operation_date <= action.share_credit_date <= event.stock_market_date):
                    raise ValueError('explicit share credit date required; listing date is not a substitute')

    def _register(self, action):
        from decimal import Decimal
        from core.calculation import calculate_entitlement, ShareDelivery

        lots = tuple(lot for lot in self.account.lots if lot.symbol == action.event.code)
        claim = calculate_entitlement(action.event, action.event.registration_date,
                                      sum(lot.shares for lot in lots))
        deliveries = []
        ratio = Decimal(str(action.event.stock_dividend_per_share)) + Decimal(str(action.event.reserve_to_stock_per_share))
        for lot in lots:
            shares = Decimal(lot.shares) * ratio
            if shares != shares.to_integral_value():
                raise ValueError('fractional lot allocation requires source evidence; no rounding')
            if shares:
                deliveries.append(ShareDelivery(lot.lot_id, lot.acquired, int(shares),
                                                action.share_credit_date, action.event.stock_market_date))
        self.distributions.register(action.distribution_id, claim, action.source_sha256, tuple(deliveries))
        return RegistrationRecord(action, claim, tuple(deliveries))

    def _apply(self, record, following, trade_start=None):
        from dataclasses import replace
        from decimal import Decimal
        from protocol import AccountSnapshot
        from core.calculation import OpeningRiskSnapshot

        if self._pending is not None:
            raise ValueError("plan is required before the next apply")
        if self._following is not None and record.trade_date != self._following:
            raise ValueError("record trade_date must match previous following session")
        if self._last_day is not None and record.trade_date <= self._last_day:
            raise ValueError("history days must move forward")
        if following <= record.trade_date:
            raise ValueError("following session must be chronological")
        self._validate_day(record, record.trade_date)
        day = record.trade_date
        due_dates = {p.due for p in self.taxes.pending.values() if p.due <= day}
        due_dates.update(self.distributions.due_dates(day))
        for when in sorted(due_dates | {day}):
            if self.account.as_of is not None and when < self.account.as_of:
                continue
            self.postings.extend(self.distributions.advance(when))
            self.taxes.advance(when)
            for payment in self.taxes.settle(when):
                self.account.record_cash(payment.event_id, payment.due, "dividend_tax", -payment.amount, payment.source_sha256)
        self.trade_rules.tax(day, phase='begin')
        claims = self.distributions.receivable_value(day, record.opening_marks)
        new_fills = self.execution.execute_open(
            day,
            record.bars,
            record.previous_closes,
            risk=OpeningRiskSnapshot(
                record.liquidity_as_of,
                record.opening_marks,
                record.opening_amount20,
                claims,
                self.trade_rules.tax(day, phase='liability'),
                self.limits,
            ),
            volume20=record.opening_volume20,
            cash_reserve=Decimal(0),
            eligible=record.opening_eligible,
        )
        self.trade_rules.tax(day, new_fills, phase='end')
        for payment in self.taxes.settle(day):
            self.account.record_cash(payment.event_id, day, "dividend_tax", -payment.amount, payment.source_sha256)
        claims = self.distributions.receivable_value(day, record.closing_marks)
        point = self.account.mark(day, record.closing_marks, claims, self.trade_rules.tax(day, phase='liability'))
        self.tax_liabilities.append((day, self.trade_rules.tax(day, phase='liability')))
        for action in record.registrations:
            self.registrations.append(self._register(action))
            self.taxes.register(
                action.distribution_id,
                action.event,
                action.stock_nominal,
                action.reserve_taxable,
                action.tax_source_sha256,
                credit_date=action.share_credit_date,
            )
        actual = {}
        for lot in self.account.lots:
            actual[lot.symbol] = actual.get(lot.symbol, 0) + lot.shares
        tax_count = {}
        for lot in self.taxes.lots:
            tax_count[lot.symbol] = tax_count.get(lot.symbol, 0) + lot.shares
        if actual != tax_count:
            raise ValueError("account and tax share quantities differ")
        positions = {}
        sellable = {}
        for lot in self.account.lots:
            positions[lot.symbol] = positions.get(lot.symbol, Decimal(0)) + lot.shares
            if lot.tradable_from <= day:
                sellable[lot.symbol] = sellable.get(lot.symbol, Decimal(0)) + lot.shares
        active = trade_start is None or day >= trade_start
        frame = replace(record.frame, context={**record.frame.context, "next_session": following.isoformat(), "trading": active})
        snapshot = AccountSnapshot(frame.at, self.account.cash, point.equity, positions, sellable)
        self._pending = (record, following, frame, point, active)
        self._last_day = day
        self._following = following
        return frame, snapshot

    def plan(self, target, strategy_id):
        self._ensure_active()
        try:
            return self._plan(target, strategy_id)
        except Exception as error:
            self.fail(error)
            raise

    def _plan(self, target, strategy_id):
        from protocol import TargetSet
        from dataclasses import replace

        if self._pending is None:
            raise ValueError("apply is required before plan")
        record, following, frame, point, active = self._pending
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError("strategy_id is required")
        if target is not None:
            if not isinstance(target, TargetSet) or target.as_of != frame.at or target.source_id != strategy_id:
                raise ValueError("strategy target identity/time do not match decision")
        if target is None:
            portfolio = TargetSet(strategy_id, frame.at, {})
        else:
            portfolio = target
        plan = self.execution.apply_target(
            portfolio,
            next_session=following,
            equity=point.equity,
            raw_closes=record.closing_marks,
            amount20=record.closing_amount20,
            limits=self.limits,
            rebalance=active and target is not None,
        )
        self.frames.append(replace(frame, history={}))
        self.targets.append(target)
        self.allocations.append(plan)
        self.points.append(point)
        self.holdings.append((record.trade_date, self.account.lots))
        self._pending = None
        return plan

    def result(self):
        return HistoryResult(
            tuple(self.frames),
            tuple(self.targets),
            tuple(self.allocations),
            self.execution.orders,
            self.execution.fills,
            self.execution.pending,
            self.account.order_cancellations,
            self.account.cash_events,
            tuple(self.points),
            tuple(self.holdings),
            tuple(self.postings),
            tuple(self.registrations),
            tuple(self.taxes.payments),
            tuple(self.tax_liabilities),
            tuple(self.taxes.pending.values()),
            self.taxes.lots,
        )
