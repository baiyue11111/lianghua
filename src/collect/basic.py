#!/usr/bin/env python3
"""Capture BaoStock's stock-basic listing dates as immutable source evidence.

The source's ``ipoDate`` is evidence for a listing anchor, not by itself proof
that two different stock codes share one adjustment-factor basis. Consumers
must verify the first price bar and code identity separately.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import json

from collect.factors import (
    ProviderError, _bs_code, _response_rows, _sha256, _timeout, login_with_timeout,
)


class StockBasicEvidenceError(ValueError):
    pass


REQUIRED_FIELDS = {"code", "ipoDate", "outDate", "type", "status"}


def _validate_rows(fields: list[str], rows: list[list[str]]) -> None:
    if len(fields) != len(set(fields)) or not REQUIRED_FIELDS <= set(fields):
        raise StockBasicEvidenceError("stock-basic fields are missing or duplicated")
    positions = {field: fields.index(field) for field in REQUIRED_FIELDS}
    seen: set[str] = set()
    for row in rows:
        if len(row) != len(fields):
            raise StockBasicEvidenceError("stock-basic row length differs from fields")
        code = row[positions["code"]]
        if not isinstance(code, str) or code in seen or not code:
            raise StockBasicEvidenceError(f"duplicate or missing stock-basic code: {code!r}")
        seen.add(code)
        if row[positions["type"]] != "1":
            continue
        ipo = row[positions["ipoDate"]]
        try:
            date.fromisoformat(ipo)
            out = row[positions["outDate"]]
            if out:
                date.fromisoformat(out)
        except (TypeError, ValueError) as exc:
            raise StockBasicEvidenceError(f"invalid IPO or delisting date for {code}") from exc


def collect_stock_basic(provider, *, timeout: int, code: str) -> dict:
    requested = _bs_code(code)
    with _timeout(timeout):
        response = provider.query_stock_basic(code=requested)
    if str(getattr(response, "error_code", "")) != "0":
        raise ProviderError(
            f"stock-basic query failed: {getattr(response, 'error_code', '')} "
            f"{getattr(response, 'error_msg', '')}",
            str(getattr(response, "error_code", "")),
        )
    fields, rows = _response_rows(response, timeout)
    _validate_rows(fields, rows)
    if len(rows) != 1:
        raise StockBasicEvidenceError(f"expected exactly one row for {requested}")
    if rows[0][fields.index("code")] != requested:
        raise StockBasicEvidenceError(f"stock-basic code mismatch for {requested}")
    raw = {
        "error_code": str(response.error_code),
        "error_msg": str(response.error_msg),
        "fields": fields,
        "rows": rows,
    }
    return {
        "schema": 1,
        "request": f"query_stock_basic({requested})",
        "raw_response": raw,
        "raw_response_sha256": _sha256(raw),
        "collected_at": datetime.now(UTC).isoformat(),
    }


def read_stock_basic(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise StockBasicEvidenceError("unsupported stock-basic snapshot schema")
    request = payload.get("request")
    if not isinstance(request, str) or not request.startswith("query_stock_basic(") or not request.endswith(")"):
        raise StockBasicEvidenceError("stock-basic request mismatch")
    requested = request[len("query_stock_basic("):-1]
    try:
        if _bs_code(requested) != requested:
            raise StockBasicEvidenceError("stock-basic requested code is not canonical")
    except ValueError as exc:
        raise StockBasicEvidenceError("stock-basic requested code is invalid") from exc
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256(raw):
        raise StockBasicEvidenceError("stock-basic response hash mismatch")
    if raw.get("error_code") != "0":
        raise StockBasicEvidenceError("stock-basic source recorded a provider error")
    fields, rows = raw.get("fields"), raw.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise StockBasicEvidenceError("stock-basic rows or fields malformed")
    _validate_rows(fields, rows)
    if len(rows) != 1 or rows[0][fields.index("code")] != requested:
        raise StockBasicEvidenceError("stock-basic response does not match requested code")
    return payload


def write_stock_basic(data, payload: dict):
    read_stock_basic(payload)
    value = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return data.write('sources', 'stock_basic', f'{_sha256(payload)}.json', value)
