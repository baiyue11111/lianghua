"""Fetch methods with standard DataBus delivery and process logging."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from math import isfinite
import duckdb
import warnings
from protocol import (DataError, normalize_security_id, validate_facts,
                      _calendar_facts, _calendar_covers, _fact_date as fact_date)
import re
import calendar
import hashlib
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
from collections.abc import Mapping
from uuid import uuid4
from typing import Callable
from urllib.parse import urlencode, urljoin
from urllib.request import Request as URLRequest, urlopen

DEFAULT_TIMEOUT = 30

FIELDSETS = {
    "basic": ("open", "high", "low", "close", "volume", "amount",
              "turn", "pctChg", "tradestatus", "isST"),
    "st": ("close", "turn", "pctChg", "tradestatus", "isST"),
    "valuation": ("close", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST"),
    "full": ("open", "high", "low", "close", "volume", "amount", "turn",
             "pctChg", "tradestatus", "isST", "peTTM", "pbMRQ", "psTTM",
             "pcfNcfTTM"),
}


class ProviderError(RuntimeError):
    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class CacheIntegrityError(RuntimeError):
    pass


class UnsupportedMarketError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    kind: str
    code: str
    start: str | None
    end: str | None
    year: str | None

    @property
    def key(self) -> str:
        code = normalize_security_id(self.code)
        if self.kind == "adjust":
            return f"adjust_{code}_{self.start}_{self.end}"
        return f"dividend_{code}_{self.year}"


def _bs_code(code: str) -> str:
    if "." in code:
        market, number = code.lower().split(".", 1)
        valid = (market == "sh" and number.startswith("6")) or (
            market == "sz" and number.startswith(("000", "001", "002", "003", "300", "301", "302")))
        if not valid or len(number) != 6 or not number.isdigit():
            raise UnsupportedMarketError(f"unsupported market code: {code}")
        return f"{market}.{number}"
    if len(code) != 6 or not code.isdigit():
        raise UnsupportedMarketError(f"unsupported market code: {code}")
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return f"sz.{code}"
    raise UnsupportedMarketError(f"unsupported market code: {code}")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _calendar_coverage(ranges):
    parsed = []
    if not isinstance(ranges, list):
        raise ValueError("calendar coverage must be a list")
    for item in ranges:
        if not isinstance(item, Mapping) or set(item) != {"start", "end"}:
            raise ValueError("calendar coverage range is malformed")
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if start.isoformat() != item["start"] or end.isoformat() != item["end"] or start > end:
            raise ValueError("calendar coverage range is invalid")
        parsed.append((start, end))
    merged = []
    for start, end in sorted(parsed):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [{"start": start.isoformat(), "end": end.isoformat()} for start, end in merged]


def _calendar_payload(value):
    if not isinstance(value, Mapping) or set(value) != {"sessions", "coverage"}:
        raise ValueError("calendar must contain sessions and coverage")
    sessions = value["sessions"]
    if not isinstance(sessions, list):
        raise ValueError("calendar sessions must be a list")
    days = [date.fromisoformat(str(day)) for day in sessions]
    if any(day.isoformat() != raw for day, raw in zip(days, sessions)):
        raise ValueError("calendar session is invalid")
    return {"sessions": sorted({day.isoformat() for day in days}),
            "coverage": _calendar_coverage(value["coverage"])}


@contextmanager
def _timeout(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)
    old_alarm = signal.alarm(0)
    def alarm_handler(_signum, _frame):
        raise TimeoutError(f"provider query exceeded {seconds}s")
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if old_alarm:
            signal.alarm(old_alarm)


def _success(response) -> bool:
    return str(getattr(response, "error_code", "")) == "0"


def _response_rows(response, timeout: int) -> tuple[list[str], list[list[str]]]:
    fields = [str(field) for field in getattr(response, "fields", [])]
    rows = []
    with _timeout(timeout):
        while response.next():
            rows.append(list(response.get_row_data()))
    if not _success(response):
        raise ProviderError(f"response iteration failed: {response.error_code} {response.error_msg}",
                            str(response.error_code))
    if any(len(row) != len(fields) for row in rows):
        raise ProviderError("response row length differs from fields")
    return fields, rows


def fetch_one(request: Request, bs, timeout: int = DEFAULT_TIMEOUT) -> dict:
    code = _bs_code(request.code)
    with _timeout(timeout):
        if request.kind == "adjust":
            response = bs.query_adjust_factor(code=code, start_date=request.start, end_date=request.end)
        elif request.kind == "dividend":
            response = bs.query_dividend_data(code=code, year=request.year, yearType="operate")
        else:
            raise ValueError(f"unsupported provider kind: {request.kind}")
    if not _success(response):
        raise ProviderError(f"{request.kind} {code} failed: {response.error_code} {response.error_msg}",
                            str(response.error_code))
    fields, rows = _response_rows(response, timeout)
    if "code" not in fields or any(row[fields.index("code")] != code for row in rows):
        raise ProviderError("provider row code does not match request")
    request_payload = {"kind": request.kind, "code": code, "start": request.start,
                       "end": request.end, "year": request.year}
    raw = {"error_code": str(response.error_code), "error_msg": str(response.error_msg),
           "code": str(getattr(response, "code", "")).lower(), "fields": fields, "rows": rows}
    return {"schema": 2, "status": "ok" if rows else "empty", "request": request_payload,
            "request_sha256": _sha256(request_payload),
            "request_fields_sha256": hashlib.sha256(",".join(fields).encode()).hexdigest(),
            "raw_response": raw, "raw_response_sha256": _sha256(raw),
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def fetch_and_cache(request: Request, bs, data, timeout: int = DEFAULT_TIMEOUT):
    payload = fetch_one(request, bs, timeout)
    facts = normalize_facts(payload)
    validate_facts(facts, kind=facts["kind"], code=facts["code"],
                   start=facts["start"], end=facts["end"], year=facts["year"])
    data.write("state", "collect", f"{uuid4().hex}.json",
               (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return data.write("sources", "facts", f"{request.key}.json",
                      (json.dumps(facts, ensure_ascii=False, indent=2) + "\n").encode())


def _fact_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise CacheIntegrityError("provider date is not text")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise CacheIntegrityError("provider date is invalid") from error


def _fact_number(value, *, positive=False):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CacheIntegrityError("provider number is invalid") from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise CacheIntegrityError("provider number is invalid")
    return number


def normalize_facts(payload: dict) -> dict:
    """Validate a provider envelope and convert its rows to standard facts."""
    if not isinstance(payload, dict):
        raise CacheIntegrityError("provider payload is not an object")
    if payload.get("schema") == 1:
        request = payload.get("request")
        prefix, suffix = "query_stock_basic(", ")"
        if not isinstance(request, str) or not request.startswith(prefix) or not request.endswith(suffix):
            raise CacheIntegrityError("stock-basic request mismatch")
        try:
            code = _bs_code(request[len(prefix):-1])
        except ValueError as error:
            raise CacheIntegrityError("stock-basic code is invalid") from error
        if code != request[len(prefix):-1]:
            raise CacheIntegrityError("stock-basic code is not canonical")
        raw = payload.get("raw_response")
        if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256(raw):
            raise CacheIntegrityError("stock-basic raw response hash mismatch")
        if raw.get("error_code") != "0":
            raise CacheIntegrityError("stock-basic response contains an error")
        fields, rows = raw.get("fields"), raw.get("rows")
        required = ("code", "ipoDate", "outDate", "type", "status")
        if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields) or
                len(fields) != len(set(fields)) or
                not isinstance(rows, list) or set(required) - set(fields) or len(rows) != 1):
            raise CacheIntegrityError("stock-basic fields or rows are malformed")
        positions = {name: fields.index(name) for name in required}
        row = rows[0]
        if not isinstance(row, list) or len(row) != len(fields) or row[positions["code"]] != code:
            raise CacheIntegrityError("stock-basic row identity or length mismatch")
        if not all(isinstance(row[positions[name]], str) for name in ("type", "status")):
            raise CacheIntegrityError("stock-basic type or status is invalid")
        record = {"listing_date": _fact_date(row[positions["ipoDate"]]),
                  "delisting_date": _fact_date(row[positions["outDate"]]),
                  "type": row[positions["type"]], "status": row[positions["status"]]}
        return {"schema": 1, "kind": "security", "code": code, "start": None, "end": None,
                "year": None, "source_hash": payload["raw_response_sha256"], "records": [record]}
    kind = payload.get("request", {}).get("kind") if isinstance(payload.get("request"), dict) else None
    request = payload.get("request")
    if payload.get("schema") != 2 or kind not in {"adjust", "dividend"}:
        raise CacheIntegrityError("unsupported provider payload")
    try:
        code = _bs_code(request.get("code", ""))
    except (AttributeError, TypeError, ValueError) as error:
        raise CacheIntegrityError("provider request code is invalid") from error
    expected = {"kind": kind, "code": code,
                "start": request.get("start"), "end": request.get("end"),
                "year": request.get("year")}
    if request != expected or payload.get("request_sha256") != _sha256(expected):
        raise CacheIntegrityError("provider request mismatch")
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256(raw):
        raise CacheIntegrityError("provider raw response hash mismatch")
    if str(raw.get("error_code")) != "0":
        raise CacheIntegrityError("provider response contains an error")
    fields, rows = raw.get("fields"), raw.get("rows")
    if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields) or
            len(fields) != len(set(fields)) or
            not isinstance(rows, list) or "code" not in fields):
        raise CacheIntegrityError("provider fields or rows are malformed")
    if raw.get("code") != expected["code"]:
        raise CacheIntegrityError("provider response code mismatch")
    if payload.get("request_fields_sha256") != hashlib.sha256(",".join(fields).encode()).hexdigest():
        raise CacheIntegrityError("provider field hash mismatch")
    if payload.get("status") not in {"ok", "empty"} or (payload["status"] == "empty") != (not rows):
        raise CacheIntegrityError("provider status mismatch")
    names = ("dividOperateDate", "foreAdjustFactor", "backAdjustFactor") if kind == "adjust" else (
        "dividPlanAnnounceDate", "dividRegistDate", "dividOperateDate", "dividPayDate",
        "dividStockMarketDate", "dividCashPsBeforeTax", "dividStocksPs",
        "dividReserveToStockPs", "dividCashPsAfterTax")
    if set(names) - set(fields):
        raise CacheIntegrityError("provider fields are missing required facts")
    positions = {name: fields.index(name) for name in ["code", *names]}
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(fields) or row[positions["code"]] != expected["code"]:
            raise CacheIntegrityError("provider row identity or length mismatch")
        if kind == "adjust":
            records.append({"date": _fact_date(row[positions["dividOperateDate"]]),
                            "forward": _fact_number(row[positions["foreAdjustFactor"]], positive=True),
                            "backward": _fact_number(row[positions["backAdjustFactor"]], positive=True)})
        else:
            records.append({"announce_date": _fact_date(row[positions["dividPlanAnnounceDate"]]),
                            "registration_date": _fact_date(row[positions["dividRegistDate"]]),
                            "operation_date": _fact_date(row[positions["dividOperateDate"]]),
                            "payment_date": _fact_date(row[positions["dividPayDate"]]),
                            "stock_market_date": _fact_date(row[positions["dividStockMarketDate"]]),
                            "cash_per_share": _fact_number(row[positions["dividCashPsBeforeTax"]]),
                            "stock_dividend_per_share": _fact_number(row[positions["dividStocksPs"]]),
                            "reserve_to_stock_per_share": _fact_number(row[positions["dividReserveToStockPs"]]),
                            "cash_after_tax_text": row[positions["dividCashPsAfterTax"]]})
    return {"schema": 1, "kind": kind, "code": expected["code"], "start": expected["start"],
            "end": expected["end"], "year": expected["year"],
            "source_hash": payload["raw_response_sha256"], "records": records}



class DividendHistory:
    """Public-market distribution quantities from Sina, settlement dates from standard facts."""

    @staticmethod
    def normalize(base, html):
        from io import StringIO
        import pandas as pd
        validate_facts(base, kind="dividend", code=base["code"], year=base["year"])
        tables = [table for table in pd.read_html(StringIO(html)) if "转增" in str(table.columns)]
        if len(tables) != 1:
            raise ValueError("one dividend history table is required")
        table = tables[0]
        if "每10股" not in str(table.columns):
            raise ValueError("dividend quantities must declare the per-ten-share unit")
        table.columns = [name[-1] if isinstance(name, tuple) else name for name in table.columns]
        fields = {"送股(股)", "转增(股)", "派息(税前)(元)", "进度", "除权除息日", "股权登记日", "红股上市日"}
        if len(set(table.columns)) != len(table.columns) or not fields <= set(table.columns):
            raise ValueError("dividend history fields are missing or duplicated")
        result = deepcopy(base)
        for record in result["records"]:
            matches = table[(table["除权除息日"].astype(str) == record["operation_date"])
                            & (table["股权登记日"].astype(str) == record["registration_date"])
                            & (table["进度"].astype(str) == "实施")]
            if len(matches) != 1:
                raise ValueError("distribution requires an exact record-date and ex-date match")
            row = matches.iloc[0]
            for target, source in (("cash_per_share", "派息(税前)(元)"),
                                   ("stock_dividend_per_share", "送股(股)"),
                                   ("reserve_to_stock_per_share", "转增(股)")):
                value = _fact_number(row[source])
                if value is None or value < 0:
                    raise ValueError("distribution quantity must be explicitly numeric")
                record[target] = float(Decimal(str(value)) / Decimal(10))
            market = str(row["红股上市日"])
            if market != "--":
                parsed = _fact_date(market)
                if record["stock_market_date"] not in (None, parsed):
                    raise ValueError("conflicting share listing date")
                record["stock_market_date"] = parsed
        result["source_hash"] = _sha256({"base": base, "html": html})
        validate_facts(result, kind="dividend", code=result["code"], year=result["year"])
        return result

    def collect(self, data, code, start_year, end_year):
        import requests
        code = normalize_security_id(code)
        url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{code.split('.')[1]}.phtml"
        response = requests.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        outputs = []
        for year in range(start_year, end_year + 1):
            name = f"dividend_{code}_{year}.json"
            base = json.loads(data.read("sources", "facts", name))
            fact = self.normalize(base, response.text)
            outputs.append((name, base, fact))
        # Validate the whole requested range before publishing any replacement.
        with data.transaction("sources"):
            for name, base, fact in outputs:
                data.write("state", "collect", f"{uuid4().hex}.json",
                    _canonical({"url": url, "source_hash": fact["source_hash"],
                                "base": base, "html": response.text}))
                data.write("sources", "facts", name, _canonical(fact))
        return {"code": code, "years": len(outputs),
                "records": sum(len(fact["records"]) for _, _, fact in outputs)}

def _validate_cache(request: Request, payload: dict) -> None:
    try:
        validate_facts(payload, kind=request.kind, code=normalize_security_id(request.code),
                       start=request.start, end=request.end, year=request.year)
    except (ValueError, TypeError) as error:
        raise CacheIntegrityError(str(error)) from error


def pending_requests(requests: list[Request], data) -> list[Request]:
    from protocol import DataError
    pending = []
    for request in requests:
        try:
            payload = json.loads(data.read("sources", "facts", f"{request.key}.json"))
        except DataError as error:
            if error.code == "missing":
                pending.append(request)
                continue
            raise CacheIntegrityError(str(error)) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CacheIntegrityError(str(error)) from error
        _validate_cache(request, payload)
    return pending


def limit_pending(requests: list[Request], data, max_tasks: int) -> list[Request]:
    pending = pending_requests(requests, data)
    return pending[:max_tasks] if max_tasks else pending


def login_with_timeout(bs, timeout: int):
    with _timeout(timeout):
        login = bs.login()
    if str(getattr(login, "error_code", "")) != "0":
        raise ProviderError(f"login failed: {login.error_code} {login.error_msg}", str(login.error_code))
    return login


BAR_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def canonical(value):
    return _canonical(value)


def digest(value):
    return _sha256(value)


def make_request(code, exchange, start, end, basis):
    if exchange not in {"SH", "SZ", "BJ"} or len(code) != 6 or not code.isdigit():
        raise ValueError("invalid security identity")
    datetime.strptime(start, "%Y%m%d"); datetime.strptime(end, "%Y%m%d")
    if start > end or basis not in {"raw", "hfq"}:
        raise ValueError("invalid dates or price basis")
    return {"provider": "eastmoney", "url": BAR_URL, "code": code,
            "exchange": exchange, "price_basis": basis, "params": {
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "0" if basis == "raw" else "2",
                "secid": f'{1 if exchange == "SH" else 0}.{code}', "beg": start, "end": end}}


def snapshot(request, raw):
    return {"schema": 1, "request": request, "request_sha256": digest(request),
            "raw_response": raw, "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def validate_snapshot(evidence, request):
    if evidence.get("schema") != 1 or evidence.get("request") != request:
        raise ValueError("snapshot request mismatch")
    if evidence.get("request_sha256") != digest(request):
        raise ValueError("request hash mismatch")
    raw = evidence.get("raw_response")
    if not isinstance(raw, str) or evidence.get("raw_response_sha256") != hashlib.sha256(raw.encode()).hexdigest():
        raise ValueError("response hash mismatch")


def parse_bars(evidence):
    request = evidence["request"]
    validate_snapshot(evidence, request)
    response = json.loads(evidence["raw_response"])
    if response.get("rc") != 0:
        raise ValueError("provider error")
    data = response.get("data")
    if data is None:
        return []
    if data.get("code") != request["code"]:
        raise ValueError("provider code mismatch")
    bars, previous = [], None
    for line in data["klines"]:
        fields = line.split(",")
        if len(fields) != 11:
            raise ValueError("unexpected kline fields")
        day = datetime.strptime(fields[0], "%Y-%m-%d").date().isoformat()
        if previous is not None and day <= previous:
            raise ValueError("duplicate or unordered date")
        if not request["params"]["beg"] <= day.replace("-", "") <= request["params"]["end"]:
            raise ValueError("date outside request")
        previous = day
        op, close, high, low, hands, amount = map(float, fields[1:7])
        if not all(math.isfinite(value) for value in (op, close, high, low, hands, amount)):
            raise ValueError("nonfinite bar")
        if min(op, close, low) <= 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("invalid OHLC")
        if hands < 0 or amount < 0 or ((hands == 0) != (amount == 0)):
            raise ValueError("inconsistent turnover")
        bars.append({"code": request["code"], "date": day, "open": op, "close": close,
                     "high": high, "low": low, "volume": hands * 100, "amount": amount,
                     "price_basis": request["price_basis"]})
    return bars


def fetch_bars(request):
    from urllib.parse import urlencode
    response = urlopen(URLRequest(request["url"] + "?" + urlencode(request["params"]),
                                  headers={"User-Agent": "Mozilla/5.0"}), timeout=20)
    with response:
        return response.read().decode("utf-8")


def capture(data, securities, start, end, basis, *, master_sha256,
            identity="bar_evidence", fetcher=None):
    transport = fetch_bars if fetcher is None else fetcher
    securities = sorted(securities)
    if not securities or len(securities) != len(set(securities)):
        raise ValueError("empty or duplicate security universe")
    manifest = {"schema": 1, "release_status": "NOT_RELEASED", "master_sha256": master_sha256,
                "basis": basis, "scope": "FULL_INPUT_MASTER", "responses": []}
    failed = 0
    for code, exchange in securities:
        request = make_request(code, exchange, start, end, basis)
        name = digest(request) + ".json"
        try:
            captured = data.read("sources", identity, name)
        except Exception as error:
            if getattr(error, "code", None) != "missing":
                raise
            captured = None
        if captured is not None:
            evidence = json.loads(captured)
            validate_snapshot(evidence, request)
        else:
            try:
                evidence = snapshot(request, transport(request))
            except OSError as error:
                failed += 1
                manifest["responses"].append({"code": code, "status": "TRANSPORT_ERROR", "error": str(error)})
                continue
            data.write("sources", identity, name, canonical(evidence) + b"\n")
        record = {"code": code, "file": name,
                  "sha256": hashlib.sha256(data.read("sources", identity, name)).hexdigest()}
        try:
            bars = parse_bars(evidence)
            record.update(status="STRUCTURALLY_VALID" if bars else "EMPTY", rows=len(bars),
                          first_date=bars[0]["date"] if bars else None,
                          last_date=bars[-1]["date"] if bars else None)
            if not bars:
                failed += 1
        except (ValueError, KeyError, TypeError) as error:
            failed += 1
            record.update(status="REJECTED", error=str(error))
        manifest["responses"].append(record)
    manifest["failed_or_empty"] = failed
    data.write("sources", identity, "manifest.json", canonical(manifest) + b"\n")
    return manifest


RAW_FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus"


def _raw_code(code):
    return _bs_code(str(code))


def discrepancy_requests(records):
    """Build raw-provider checks from rows already returned by DataBus."""
    grouped = {}
    for row in records:
        values = {key: row.get(key) for key in RAW_FIELDS.split(",")}
        bad_turnover = (values["volume"] is not None and values["amount"] is not None and
                        values["volume"] > 0 and values["amount"] > 0 and
                        (values["amount"] / values["volume"] < values["low"] * .99 or
                         values["amount"] / values["volume"] > values["high"] * 1.01))
        bad_status = (values["tradestatus"] == 0 and (values["volume"] > 0 or values["amount"] > 0))
        bad_zero = (values["tradestatus"] == 1 and (values["volume"] or 0) <= 0)
        bad_unknown = (values["tradestatus"] is None and (values["volume"] or 0) == 0
                       and (values["amount"] or 0) == 0)
        bad_price = any(values[name] is not None and values[name] <= 0
                        for name in ("open", "close")) or values["high"] < values["low"]
        if bad_turnover or bad_status or bad_zero or bad_unknown or bad_price:
            key = _raw_code(values["code"])
            item = grouped.setdefault(key, {"code": key, "start_date": values["date"],
                                            "end_date": values["date"], "affected_rows": 0})
            item["start_date"] = min(item["start_date"], values["date"])
            item["end_date"] = max(item["end_date"], values["date"])
            item["affected_rows"] += 1
    return [dict(method="query_history_k_data_plus", code=item["code"], fields=RAW_FIELDS,
                 start_date=item["start_date"], end_date=item["end_date"], frequency="d",
                 adjustflag="3", affected_rows=item["affected_rows"])
            for item in sorted(grouped.values(), key=lambda value: value["code"])]


def validate_response(request, fields, rows):
    if fields != RAW_FIELDS.split(",") or not rows:
        raise ValueError("raw response fields differ or response is empty")
    previous = None
    for row in rows:
        if len(row) != len(fields):
            raise ValueError("raw response row length mismatch")
        day = date.fromisoformat(row[0])
        if (row[1] != request["code"] or not request["start_date"] <= row[0] <= request["end_date"]
                or (previous is not None and day <= previous)):
            raise ValueError("raw response identity/date mismatch or duplicate")
        numbers = [float(value) if value else math.nan for value in row[2:8]]
        if any(math.isfinite(number) and number < 0 for number in numbers):
            raise ValueError("negative raw price/turnover")
        status = float(row[8]) if row[8] else None
        if numbers[4] > 0 and numbers[5] > 0 and not numbers[2] * .99 <= numbers[5] / numbers[4] <= numbers[1] * 1.01:
            raise ValueError("turnover outside raw price range")
        if status == 0 and any(number > 0 for number in numbers[4:6]):
            raise ValueError("suspended row has turnover")
        if all(math.isfinite(number) for number in numbers[:4]):
            opening, high, low, close = numbers[:4]
            if high < max(opening, close, low) or low > min(opening, close, high):
                raise ValueError("raw OHLC range inconsistent")
        previous = day
    return len(rows)


def fetch_sina_factors(symbol: str) -> list[tuple[str, float]]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("invalid security code")
    code = ("sh" if symbol[0] in "69" else "sz") + symbol
    response = urlopen(URLRequest(f"https://finance.sina.com.cn/realstock/company/{code}/hfq.js",
                                  headers={"User-Agent": "Mozilla/5.0"}), timeout=25)
    with response:
        text = response.read().decode("utf-8")
    match = re.search(r"=\s*\{", text)
    if not match:
        raise ValueError("missing adjustment-factor payload")
    payload, _ = json.JSONDecoder().raw_decode(text[match.end() - 1:])
    return sorted((row["d"], float(row["f"])) for row in payload["data"])


def listed_names():
    import akshare
    return akshare.stock_info_a_code_name()


def raw_response(request, timeout):
    import baostock as bs
    login_with_timeout(bs, timeout)
    try:
        with _timeout(timeout):
            response = bs.query_history_k_data_plus(
                request["code"], request["fields"], start_date=request["start_date"],
                end_date=request["end_date"], frequency="d", adjustflag="3")
        fields, rows = _response_rows(response, timeout)
        return {"error_code": response.error_code, "error_msg": response.error_msg,
                "fields": fields, "rows": rows}
    finally:
        bs.logout()


def daily_frame(code, fields, start, end, frequency="d", adjustment="3"):
    import pandas as pd
    names = fields.split(",")
    request = {"codes": [_bs_code(code)], "fields": names, "start": start, "end": end,
               "frequency": frequency, "adjustment": adjustment}
    result = BaostockDaily({}).collect(request)
    return pd.DataFrame(result["records"], columns=names)


def factor_frame(code, start, end):
    import pandas as pd
    request = {"code": _bs_code(code), "fields": "code,dividOperateDate,foreAdjustFactor",
               "start_date": start, "end_date": end}
    raw = raw_response(request, 30)
    return pd.DataFrame(raw["rows"], columns=raw["fields"])


class Methods:
    """Resolve a registered method, validate its packet, and publish it."""

    FIELDSETS = FIELDSETS

    def __init__(self, data, handlers: Mapping[str, object] | None = None):
        self.data = data
        self.handlers = dict(handlers or {})

    @staticmethod
    def _request_items(value, name):
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise ValueError(f"{name} must be comma text or a list")
        result = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name} contains an empty item")
            item = item.strip()
            if item not in result:
                result.append(item)
        if not result:
            if name == "codes" and not isinstance(value, str):
                return []
            raise ValueError(f"{name} is required")
        return result

    @classmethod
    def prepare_request(cls, source: Mapping, request: Mapping) -> dict:
        if not isinstance(source, Mapping) or not isinstance(request, Mapping):
            raise TypeError("source and request must be objects")
        value = deepcopy(dict(source.get("defaults", {})))
        if "fields" not in value and "fields" in source:
            value["fields"] = deepcopy(source["fields"])
        value.update(deepcopy(dict(request)))
        if "codes" in value:
            value["codes"] = cls._request_items(value["codes"], "codes")
        if "fields" in value:
            fields = cls._request_items(value["fields"], "fields")
            if source.get('data_type') not in {'daily', 'daily_factors'} and source.get('method') != 'daily':
                value['fields'] = fields
            elif len(fields) == 1 and fields[0] in cls.FIELDSETS:
                if fields[0] == "full":
                    declared = cls._request_items(source["fields"], "source fields") \
                        if "fields" in source else []
                    declared = {field for field in declared if field not in {"date", "code"}}
                    required = set(cls.FIELDSETS["full"])
                    if declared and not required <= declared:
                        raise ValueError("full fields exceed declared provider capability")
                    fields = list(cls.FIELDSETS["full"])
                else:
                    fields = list(cls.FIELDSETS[fields[0]])
            elif any(item in cls.FIELDSETS for item in fields):
                raise ValueError("fields preset cannot be combined with field names")
            if source.get('data_type') in {'daily', 'daily_factors'} or source.get('method') == 'daily':
                value["fields"] = [field for field in fields if field not in {"date", "code"}]
        if "start" in value or "end" in value:
            if not isinstance(value.get("start"), str) or not isinstance(value.get("end"), str):
                raise ValueError("start and end are required together")
            try:
                start, end = date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
            except ValueError as error:
                raise ValueError("start and end must be ISO dates") from error
            if start > end:
                raise ValueError("start must not exceed end")
        return value

    def fetch(self, source: Mapping, request: Mapping) -> dict:
        method_id = source.get("method")
        handler = self.handlers.get(method_id, DEFAULT_METHODS.get(method_id))
        if handler is None:
            raise KeyError(method_id)
        frozen_source = deepcopy(dict(source))
        frozen_request = deepcopy(dict(request))
        date_field = getattr(handler, 'date_field', None)
        if date_field is not None and not request.get('force'):
            try:
                current = json.loads(self.data.read('sources', source['id'], 'records.json'))['records']
            except DataError as error:
                if error.code != 'missing':
                    raise
                current = []
            for row in current:
                handler.validate_record(row)
            frozen_request['completed_dates'] = sorted({row[date_field] for row in current})
        if isinstance(handler, type):
            packet = handler(frozen_source).collect(frozen_request)
        elif callable(handler):
            packet = handler(frozen_source, frozen_request)
        else:
            packet = handler.collect(frozen_request)
        if not isinstance(packet, dict):
            raise TypeError("method must return a data packet")
        if not isinstance(packet.get("records"), list) or not isinstance(packet.get("raw"), list):
            raise ValueError("data packet must contain records and raw lists")
        if not isinstance(packet.get("scope"), Mapping):
            raise ValueError("data packet scope is required")
        result = deepcopy(packet)
        result["source_id"] = source["id"]
        result['request'] = {key: deepcopy(value) for key, value in frozen_request.items()
                             if key != 'completed_dates'}
        for name in ("data_type", "storage_id", "dataset_id"):
            if name in source and name not in result:
                result[name] = deepcopy(source[name])
        for name in ("frequency", "price_basis", "adjustment", "fields"):
            if name in frozen_request and name not in result:
                result[name] = deepcopy(frozen_request[name])
        if source.get('method') == 'daily' or result.get("data_type") == "daily":
            packet_calendar = result.get("calendar")
            stored_name = source.get("calendar")
            stored = None
            if isinstance(stored_name, str):
                calendar_dataset = result.get("dataset_id") or source.get("dataset_id")
                payload = json.loads(self.data.read("sources", calendar_dataset, stored_name))
                stored = _calendar_payload(payload)
            elif stored_name is not None:
                raise ValueError("daily source calendar must name a calendar file")
            actual = _calendar_payload(packet_calendar) if packet_calendar is not None else None
            if stored is not None or actual is not None:
                sources = [item for item in (stored, actual) if item is not None]
                result["calendar"] = {
                    "sessions": sorted({day for item in sources for day in item["sessions"]}),
                    "coverage": _calendar_coverage([span for item in sources for span in item["coverage"]]),
                }
        return result

    def coverage(self, source: Mapping, request: Mapping):
        if (source.get("data_type") != "daily" and source.get('method') != 'daily') or request.get("force") is True:
            return None
        try:
            from protocol import DataError
            from datetime import date
            symbols = tuple(request.get("codes", ()))
            fields = tuple(request.get("fields") or source.get("fields") or
                           ("open", "high", "low", "close", "volume", "amount"))
            return self.daily_coverage(
                source.get("dataset_id", source["id"]), symbols,
                date.fromisoformat(request["start"]), date.fromisoformat(request["end"]), fields)
        except DataError as error:
            if error.code == "missing":
                return None
            raise

    def daily_coverage(self, dataset: str, symbols: tuple[str, ...],
                       start: date, end: date, fields: tuple[str, ...]) -> Mapping[str, object]:
        """Return observed daily keys without assembling a backtest Dataset."""
        if (not symbols or not fields or start > end or
                any(not isinstance(value, str) or not value for value in (*symbols, *fields))):
            raise DataError("invalid_query", "daily coverage arguments are invalid")
        with self.data.transaction('sources'):
            manifest = self.data.manifest(dataset)
            calendar_name = manifest.get("calendar")
            calendar = json.loads(self.data.read("sources", dataset, calendar_name))
            sessions = tuple(date.fromisoformat(str(value))
                             for value in calendar.get("sessions", ()))
            wanted_days = tuple(day for day in sessions if start <= day <= end)
            wanted = {(normalize_security_id(symbol), day.isoformat()) for symbol in symbols for day in wanted_days}
            aliases = sorted(set(symbols) | {normalize_security_id(symbol) for symbol in symbols}
                             | {symbol for symbol in manifest['scope']['symbols']
                                if normalize_security_id(symbol) in {normalize_security_id(item) for item in symbols}})
            found = set()
            import pyarrow as pa
            import pyarrow.parquet as pq
            years = {str(day.year) for day in wanted_days}
            columns = ["code", "date", *fields]
            columns = list(dict.fromkeys(columns))
            for ref in manifest.get("files", ()):
                name = ref.get("file", "")
                parts = Path(name).parts
                if not name.endswith(".parquet") or "daily" not in parts:
                    continue
                stem = Path(parts[-1]).stem
                if stem.isdigit() and len(stem) == 4 and stem not in years:
                    continue
                daily_index = parts.index("daily")
                identity = parts[1] if parts[0] == "source" else dataset
                relative = Path(*parts[daily_index:]).as_posix()
                payload = self.data.read("sources", identity, relative)
                parquet = pq.ParquetFile(pa.BufferReader(payload))
                names = set(parquet.schema_arrow.names)
                if not set(columns) <= names:
                    continue
                date_type = parquet.schema_arrow.field("date").type
                lower = start if pa.types.is_date(date_type) else start.isoformat()
                upper = end if pa.types.is_date(date_type) else end.isoformat()
                table = pq.read_table(
                    pa.BufferReader(payload), columns=columns,
                    filters=[("code", "in", aliases), ("date", ">=", lower),
                             ("date", "<=", upper)],
                )
                for row in table.to_pylist():
                    key = (normalize_security_id(str(row.get("code"))), str(row.get("date")))
                    if key in wanted and all(row.get(field) is not None for field in fields):
                        found.add(key)
            missing = {
                symbol: [day.isoformat() for day in wanted_days
                         if (normalize_security_id(symbol), day.isoformat()) not in found]
                for symbol in symbols
            }
            calendar_complete = bool(sessions) and sessions[0] <= start <= end < sessions[-1]
            return {"dataset": dataset, "start": start.isoformat(), "end": end.isoformat(),
                    "calendar_complete": calendar_complete,
                    "fields": tuple(fields), "complete": bool(wanted_days) and calendar_complete and not any(missing.values()),
                    "missing": missing, "sessions": tuple(day.isoformat() for day in wanted_days)}

    @staticmethod
    def missing_request(request: Mapping, coverage: Mapping) -> dict:
        if not coverage["calendar_complete"]:
            return dict(request)
        missing = coverage.get("missing", {})
        days = [day for values in missing.values() for day in values]
        codes = [code for code, values in missing.items() if values]
        if not days or not codes:
            return dict(request)
        value = dict(request)
        value["codes"] = codes
        value["start"], value["end"] = min(days), max(days)
        return value

    def prepare(self, source: Mapping, packet: Mapping):
        return self._deliver(source, packet, dry_run=True)

    def publish(self, source: Mapping, packet: Mapping):
        result = self._deliver(source, packet, dry_run=False)
        if not packet['records']:
            return result
        try:
            view = self.records(source, 0, 0)
            self.log({'source_id': source['id'], 'snapshot_id': view['snapshot_id'],
                      'raw': packet['raw'], 'request': packet.get('request', {}),
                      'scope': packet.get('scope', {}),
                      'incoming': [{'record_hash': _sha256({key: value for key, value in row.items()
                                                          if key != 'raw_page'}),
                                    'raw_page': row.get('raw_page')}
                                   for row in packet['records']],
                      'collected_at': datetime.now(timezone.utc).isoformat()})
        except Exception as error:
            warnings.warn(f'published source evidence log failed: {error}', RuntimeWarning)
        return result

    def _deliver(self, source, packet, *, dry_run):
        self.validate(packet)
        handler = self.handlers.get(source.get('method'), DEFAULT_METHODS.get(source.get('method')))
        validate_record = getattr(handler, 'validate_record', None)
        if validate_record is not None:
            for row in packet['records']:
                validate_record(row)
        if source.get('method') == 'daily' or source.get("data_type") == "daily":
            return self._publish_daily(source["storage_id"], source["dataset_id"], packet, dry_run=dry_run)
        handler = self.handlers.get(source.get('method'), DEFAULT_METHODS.get(source.get("method")))
        declared = source.get('params', {}).get('key_fields', getattr(handler, 'keys', None))
        if (not isinstance(declared, (list, tuple)) or not declared
                or any(not isinstance(name, str) or not name for name in declared)
                or len(set(declared)) != len(declared)):
            raise DataError('identity_policy', 'source must declare business key_fields')
        keys = tuple(declared)
        incoming = packet["records"]
        if keys and any(not isinstance(row, Mapping) or any(row.get(key) is None for key in keys)
                        for row in incoming):
            raise DataError("invalid_packet", "record identity fields are required")
        def key(row):
            return tuple(_canonical(row[name]) for name in keys)
        with self.data.transaction("sources"):
            try:
                old = json.loads(self.data.read("sources", source["id"], "records.json"))["records"]
            except DataError as error:
                if error.code != "missing":
                    raise
                old = []
            merged = {}
            for index, row in enumerate([*old, *incoming]):
                value = {name: deepcopy(item) for name, item in row.items() if name != "raw_page"}
                identity = key(value)
                if identity in merged:
                    merged[identity] = self._merge_record(merged[identity], value, identity,
                        refresh=index >= len(old) and packet.get('request', {}).get('refresh') is True)
                else:
                    merged[identity] = value
            records = [merged[identity] for identity in sorted(merged)]
            content = json.dumps({"records": records}, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode()
            stats = {"records": len(records), "incoming": len(incoming)}
            if dry_run:
                return stats
            return self.data.write("sources", source["id"], "records.json", content)

    @staticmethod
    def _merge_record(old, new, identity, *, refresh=False):
        value = dict(old)
        for name in set(old) | set(new):
            left, right = old.get(name), new.get(name)
            if refresh and right is not None:
                value[name] = right
                continue
            if left is not None and right is not None and left != right:
                raise DataError("data_conflict", f"record conflict: {identity!r} {name}")
            value[name] = right if left is None else left
        return value

    def records(self, source: Mapping, offset=0, limit=50, **scope):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("invalid records page")
        if source.get('method') == 'daily' or source.get("data_type") == "daily":
            return self.daily_collection_page(source, source["dataset_id"], offset=offset,
                                              limit=limit, **scope)
        try:
            content = self.data.read("sources", source["id"], "records.json")
        except DataError as error:
            if error.code == "missing":
                return None
            raise
        rows = json.loads(content)["records"]
        dates = [str(row[name]) for row in rows for name in ("date", "data_at", "published_at")
                 if row.get(name) is not None]
        version = hashlib.sha256(content).hexdigest()
        evidence = self.evidence(version)
        start, end, keyword = scope.get('start'), scope.get('end'), scope.get('keyword', '')
        if start is not None and end is not None and date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError('start must not exceed end')
        def selected(row):
            day = next((str(row[name])[:10] for name in ('date', 'published_at', 'data_at')
                        if row.get(name) is not None), None)
            if start is not None and (day is None or day < start):
                return False
            if end is not None and (day is None or day > end):
                return False
            return not keyword or keyword in json.dumps(row, ensure_ascii=False)
        rows = [row for row in rows if selected(row)]
        page = rows[offset:offset + limit]
        raw = self.record_evidence(page[0]) if len(page) == 1 else []
        fields = scope.get('fields')
        if fields is not None:
            fields = self._request_items(fields, 'fields')
            page = [{key: value for key, value in row.items() if key in fields} for row in page]
        return {"snapshot_id": version, "source_id": source["id"],
                "collected_at": evidence.get('collected_at'), "request": evidence.get('request', {}),
                "data_as_of": max(dates) if dates else None,
                "scope": evidence.get('scope', {}), "records": page, "raw": raw, "reviews": [],
                "records_count": len(rows), "offset": offset, "limit": limit, "total": len(rows)}

    def record_evidence(self, record):
        digest = _sha256(record)
        for ref in self.data.list('state', 'collect'):
            if '/' in ref.name:
                continue
            event = json.loads(self.data.read('state', 'collect', ref.name))
            for binding in event.get('incoming', []):
                index = binding.get('raw_page')
                if (binding.get('record_hash') == digest and type(index) is int
                        and 0 <= index < len(event.get('raw', []))):
                    return [event['raw'][index]]
        return []

    def evidence(self, version):
        for ref in self.data.list('state', 'collect'):
            if '/' in ref.name:
                continue
            value = json.loads(self.data.read('state', 'collect', ref.name))
            if value.get('snapshot_id') == version and isinstance(value.get('raw'), list):
                return value
        return {}

    @staticmethod
    def _sql_name(name):
        return '"' + name.replace('"', '""') + '"'

    def _publish_daily(self, storage_id: str, dataset_id: str, packet: Mapping[str, object], *, dry_run: bool = False) -> DataRef | dict:
        """Merge a typed daily packet into existing year-partitioned Parquet files."""
        if (not isinstance(storage_id, str) or Path(storage_id).name != storage_id
                or not isinstance(dataset_id, str) or Path(dataset_id).name != dataset_id):
            raise DataError("invalid_identity", "invalid daily source or dataset identity")
        self._validate_canonical_daily(packet)
        rows = packet.get("records") if isinstance(packet, Mapping) else None
        calendar = packet.get("calendar") if isinstance(packet, Mapping) else None
        if not isinstance(rows, list) or not rows or not isinstance(calendar, Mapping):
            raise DataError("invalid_packet", "daily rows and calendar are required")
        try:
            calendar = _calendar_payload(calendar)
        except (TypeError, ValueError) as error:
            raise DataError("invalid_packet", str(error)) from error
        if not calendar["sessions"]:
            raise DataError("invalid_packet", "daily calendar has no sessions")
        from datetime import date as _date
        incoming, years = {}, set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise DataError("invalid_packet", "daily rows must be objects")
            required = {"code", "date", "open", "high", "low", "close", "volume", "amount"}
            if not required <= set(row):
                raise DataError("invalid_packet", "daily row fields are incomplete")
            try:
                day = _date.fromisoformat(str(row["date"]))
                _date.fromisoformat(str(row.get("available_at", row["date"]))[:10])
            except (TypeError, ValueError) as error:
                raise DataError("invalid_packet", "daily dates are invalid") from error
            for field in ("open", "high", "low", "close", "volume", "amount"):
                value = row[field]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not isfinite(value) or value < 0
                        or (field in {"open", "high", "low", "close"} and value <= 0)):
                    raise DataError("invalid_packet", f"daily field is invalid: {field}")
            identity = normalize_security_id(str(row["code"]))
            key = (identity, day.isoformat())
            value = {name: item for name, item in row.items() if name != "raw_page"}
            incoming[key] = self._merge_daily_row(incoming[key], value, key) if key in incoming else value
            years.add(str(day.year))
        import pyarrow as pa
        import pyarrow.parquet as pq
        with self.data.transaction('sources'):
            manifest = self.data.manifest(dataset_id)
            current_calendar = json.loads(self.data.read( "sources", dataset_id, manifest["calendar"]))
            current_calendar = _calendar_payload(current_calendar)
            known_sessions = set(current_calendar["sessions"])
            known_sessions.update(calendar["sessions"])
            known_coverage = _calendar_coverage([
                *current_calendar["coverage"], *calendar["coverage"]])
            merged_by_year, payloads = {}, {}
            for year in sorted(years):
                name = f"daily/{year}.parquet"
                old_rows, old_schema = [], None
                try:
                    old_bytes = self.data.read( "sources", storage_id, name)
                except DataError as error:
                    if error.code != "missing":
                        raise
                else:
                    old_table = pq.read_table(pa.BufferReader(old_bytes))
                    old_rows, old_schema = old_table.to_pylist(), old_table.schema
                merged = {}
                for old in old_rows:
                    key = (normalize_security_id(str(old["code"])), str(old["date"]))
                    merged[key] = self._merge_daily_row(merged[key], old, key) if key in merged else dict(old)
                for key, row in incoming.items():
                    if key[1][:4] == year:
                        merged[key] = self._merge_daily_row(merged[key], row, key,
                            refresh=packet.get('request', {}).get('refresh') is True) if key in merged else row
                merged_by_year[year] = (name, sorted(merged.values(),
                    key=lambda item: (str(item["code"]), str(item["date"]))), old_schema)
            calendar_name = manifest["calendar"]
            calendar_payload = json.dumps(
                {"sessions": sorted(known_sessions), "coverage": known_coverage},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            for year, (name, values, old_schema) in merged_by_year.items():
                fields = list(old_schema.names) if old_schema is not None else []
                for row in values:
                    for field in row:
                        if field not in fields:
                            fields.append(field)
                arrays, schema_fields = [], []
                for field in fields:
                    if old_schema is not None and field in old_schema.names:
                        arrow_type = old_schema.field(field).type
                        arrays.append(pa.array([row.get(field) for row in values], type=arrow_type))
                        schema_fields.append(pa.field(field, arrow_type))
                    else:
                        array = pa.array([row.get(field) for row in values])
                        arrays.append(array)
                        schema_fields.append(pa.field(field, array.type))
                output = pa.BufferOutputStream()
                pq.write_table(pa.Table.from_arrays(arrays, schema=pa.schema(schema_fields)), output)
                payloads[name] = output.getvalue().to_pybytes()
            if dry_run:
                return {
                    "years": sorted(merged_by_year),
                    "records": sum(len(values) for _, values, _ in merged_by_year.values()),
                    "incoming": len(incoming),
                    "calendar": len(known_sessions),
                }
            files = []
            for ref in manifest["files"]:
                name = ref["file"]
                if name == calendar_name:
                    files.append({**ref, "sha256": hashlib.sha256(calendar_payload).hexdigest()})
                elif name.startswith(f"source/{storage_id}/daily/") and name.split("/")[-1].split(".")[0] in years:
                    files.append({**ref, "sha256": hashlib.sha256(payloads[f"daily/{name.split('/')[-1]}"]).hexdigest()})
                else:
                    files.append(ref)
            known = {ref["file"] for ref in files}
            for name, payload in payloads.items():
                path = f"source/{storage_id}/{name}"
                if path not in known:
                    files.append({"file": path, "sha256": hashlib.sha256(payload).hexdigest(), "role": "market"})
            updated = dict(manifest)
            updated["files"] = files
            scope = dict(updated.get("scope", {}))
            scope["symbols"] = self._merge_symbol_scope(
                scope.get("symbols", []), [row.get("code") for row in incoming.values()])
            if known_coverage:
                scope["calendar_start"] = known_coverage[0]["start"]
                scope["calendar_end"] = known_coverage[-1]["end"]
            else:
                scope.pop("calendar_start", None)
                scope.pop("calendar_end", None)
            updated["scope"] = scope
            manifest_payload = json.dumps(updated, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            self.data.write("sources", dataset_id, calendar_name, calendar_payload)
            for name, payload in payloads.items():
                self.data.write("sources", storage_id, name, payload)
            return self.data.write("sources", dataset_id, "manifest.json", manifest_payload)

    @staticmethod
    def _merge_daily_row(old, new, key, *, refresh=False):
        value = dict(old)
        for field in set(old) | set(new):
            left, right = old.get(field), new.get(field)
            if field == "code":
                value[field] = left if left is not None else right
                continue
            if refresh and right is not None:
                value[field] = right
                continue
            if left is not None and right is not None and left != right:
                raise DataError("data_conflict", f"daily field conflict: {key[0]} {key[1]} {field}")
            value[field] = right if left is None else left
        return value

    @staticmethod
    def _merge_symbol_scope(existing, incoming):
        values = []
        identities = set()
        for symbol in [*existing, *incoming]:
            identity = normalize_security_id(str(symbol))
            if identity not in identities:
                identities.add(identity)
                values.append(str(symbol))
        return sorted(values)

    @staticmethod
    def _validate_canonical_daily(packet: Mapping[str, object]) -> None:
        request = packet.get("request") if isinstance(packet, Mapping) else None
        request = request if isinstance(request, Mapping) else {}
        frequency = packet.get("frequency", request.get("frequency", "daily"))
        if frequency in {"d", "日"}:
            frequency = "daily"
        if frequency != "daily":
            raise DataError("canonical_daily", "only unadjusted daily packets may enter the daily source")
        basis = packet.get("price_basis", request.get("price_basis", "raw"))
        if basis in {"不复权", "unadjusted"}:
            basis = "raw"
        if basis != "raw":
            raise DataError("canonical_daily", "adjusted prices cannot enter the raw daily source")
        adjustment = packet.get("adjustment", request.get("adjustment"))
        if adjustment not in (None, "3", 3, "不复权", "unadjusted"):
            raise DataError("canonical_daily", "adjusted frequency cannot enter the raw daily source")
        fields = packet.get("fields", request.get("fields"))
        if fields is not None:
            names = set(fields) if isinstance(fields, (list, tuple, set)) else set()
            required = {"open", "high", "low", "close", "volume", "amount"}
            if not required <= names:
                raise DataError("canonical_daily", "daily source packets require OHLCVA fields")


    def daily_collection_view(self, source: Mapping, dataset_id: str) -> Mapping[str, object]:
        """Return only canonical metadata; rows are fetched by page."""
        return self.daily_collection_page(source, dataset_id, offset=0, limit=0)

    def _daily_view_context(self, source: Mapping, dataset_id: str):
        source_id = source['id']
        """Read daily metadata and current file references without bars."""
        from datetime import date as _date
        with self.data.transaction('sources'):
            manifest = self.data.manifest(dataset_id)
            calendar_name = manifest.get("calendar")
            calendar = json.loads(self.data.read( "sources", dataset_id, calendar_name))
            sessions = tuple(_date.fromisoformat(str(item)) for item in calendar.get("sessions", ()))
            if len(sessions) < 2:
                raise DataError("invalid_dataset", "daily calendar requires a following session")
            scope = dict(manifest.get("scope", {}))
            dataset_symbols = tuple(scope.get("symbols", ()))
            symbols = dataset_symbols
            if not dataset_symbols:
                raise DataError("invalid_dataset", "daily scope symbols are required")
            descriptor = source
            source_codes = descriptor.get("defaults", {}).get("codes")
            if isinstance(source_codes, list) and source_codes:
                requested_codes = tuple(str(item) for item in source_codes)
                requested_ids = {normalize_security_id(item) for item in requested_codes}
                available = {normalize_security_id(item) for item in dataset_symbols}
                if not requested_ids <= available:
                    raise DataError("scope_mismatch", "registered source symbols are outside the dataset")
                symbols = requested_codes
                scope["symbols"] = list(requested_codes)
            daily_refs = [ref for ref in manifest.get("files", [])
                          if isinstance(ref, Mapping) and "daily/" in str(ref.get("file", ""))]
            generation = {"source_id": source_id, "dataset": dataset_id,
                          "scope": scope, "calendar": [day.isoformat() for day in sessions],
                          "files": sorted((ref["file"], ref["sha256"]) for ref in daily_refs)}
            snapshot_id = hashlib.sha256(json.dumps(generation, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            files = []
            for ref in daily_refs:
                parts = Path(str(ref["file"])).parts
                if len(parts) >= 4 and parts[0] == "source":
                    files.append((parts[1], Path(*parts[2:]).as_posix()))
                elif parts and parts[0] == "daily":
                    files.append((dataset_id, Path(*parts).as_posix()))
            return manifest, sessions, scope, symbols, files, snapshot_id, dataset_symbols

    def daily_collection_summary(self, source: Mapping, dataset_id: str) -> Mapping[str, object]:
        """Return SQL metadata only.  This never creates a Dataset or Bar."""
        source_id = source["id"]
        with self.data.transaction('sources'):
            manifest, sessions, scope, symbols, files, snapshot_id, dataset_symbols = self._daily_view_context(
                source, dataset_id)
            paths = [str(self.data.path("sources", identity, name)) for identity, name in files]
            if not paths:
                raise DataError("invalid_dataset", "daily Parquet files are required")
            codes = sorted({str(item) for item in symbols} |
                           {normalize_security_id(str(item)) for item in symbols} |
                           {str(item) for item in dataset_symbols
                            if normalize_security_id(str(item)) in {
                                normalize_security_id(str(code)) for code in symbols}})
            literal = ",".join("'" + item.replace("'", "''") + "'" for item in codes)
            connection = duckdb.connect()
            try:
                row = connection.execute(
                    "SELECT count(*), count(DISTINCT code), min(date), max(date) "
                    f"FROM read_parquet(?, union_by_name=true) WHERE code IN ({literal})", [paths]).fetchone()
            finally:
                connection.close()
        return {"snapshot_id": snapshot_id, "source_id": source_id,
                "collected_at": None,
                "request": {"dataset": dataset_id, "symbols": list(symbols)},
                "data_as_of": str(row[3]) if row and row[3] is not None else sessions[-2].isoformat(),
                "scope": scope, "records": [],
                "records_count": int(row[0] or 0), "symbols_count": int(row[1] or 0),
                "first_date": str(row[2]) if row and row[2] is not None else None,
                "last_date": str(row[3]) if row and row[3] is not None else None}

    def daily_collection_page(self, source: Mapping, dataset_id: str, *, offset: int = 0,
                              limit: int = 50, start=None, end=None, symbols=None) -> Mapping[str, object]:
        """Read one canonical daily page with SQL filtering and pagination."""
        source_id = source["id"]
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 0:
            raise DataError("invalid_snapshot", "invalid snapshot page")
        with self.data.transaction('sources'):
            manifest, sessions, scope, declared, files, snapshot_id, dataset_symbols = self._daily_view_context(
                source, dataset_id)
            start = sessions[0] if start is None else date.fromisoformat(str(start))
            end = sessions[-2] if end is None else date.fromisoformat(str(end))
            if not sessions[0] <= start <= end <= sessions[-2]:
                raise DataError("invalid_snapshot", "snapshot dates are outside the daily calendar")
            requested = declared if symbols is None else tuple(str(item) for item in symbols)
            requested_ids = tuple(normalize_security_id(item) for item in requested)
            declared_ids = {normalize_security_id(item) for item in declared}
            if not set(requested_ids) <= declared_ids:
                raise DataError("scope_mismatch", "snapshot symbols are outside the daily scope")
            codes = sorted({str(item) for item in requested} | set(requested_ids) |
                           {str(item) for item in dataset_symbols
                            if normalize_security_id(str(item)) in set(requested_ids)})
            literal = ",".join("'" + item.replace("'", "''") + "'" for item in codes)
            paths = [str(self.data.path("sources", identity, name)) for identity, name in files]
            connection = duckdb.connect()
            try:
                where = f"code IN ({literal}) AND CAST(date AS DATE) BETWEEN ? AND ?"
                total = connection.execute(
                    f"SELECT count(*) FROM read_parquet(?, union_by_name=true) WHERE {where}",
                    [paths, start.isoformat(), end.isoformat()]).fetchone()[0]
                rows = [] if limit == 0 else connection.execute(
                    f"SELECT * FROM read_parquet(?, union_by_name=true) WHERE {where} ORDER BY date, code LIMIT {limit} OFFSET {offset}",
                    [paths, start.isoformat(), end.isoformat()]).fetch_arrow_table().to_pylist()
            finally:
                connection.close()
        required = {"open", "high", "low", "close", "volume", "amount"}
        records = []
        for row in rows:
            value = {}
            for key, item in row.items():
                if isinstance(item, float) and not math.isfinite(item):
                    item = None
                value[key] = item.isoformat() if isinstance(item, date) else item
            for key in required:
                item = value.get(key)
                if item is None or (isinstance(item, float) and not math.isfinite(item)):
                    raise DataError("invalid_dataset", f"required daily field is not finite: {key}")
            records.append(value)
        return {"snapshot_id": snapshot_id, "source_id": source_id,
                "collected_at": None,
                "request": {"dataset": dataset_id, "symbols": list(requested),
                            "start": start.isoformat(), "end": end.isoformat()},
                "data_as_of": end.isoformat(), "scope": scope,
                "records": records, "records_count": int(total),
                "offset": offset, "limit": limit, "total": int(total)}

    @staticmethod
    def validate(packet: Mapping) -> None:
        if not isinstance(packet, Mapping):
            raise ValueError("data packet must be an object")
        if not isinstance(packet.get("records"), list) or not isinstance(packet.get("raw"), list):
            raise ValueError("data packet must contain records and raw lists")
        if not isinstance(packet.get("scope"), Mapping):
            raise ValueError("data packet scope is required")

    def log(self, event: Mapping):
        if not isinstance(event, Mapping):
            raise TypeError("log event must be an object")
        name = f"{uuid4().hex}.json"
        return self.data.write(
            "state", "collect", name,
            json.dumps(dict(event), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False).encode(),
        )


    def inspect(self, source, value):
        result = {}
        if 'codes' in value:
            result['last_date'] = self.last_date(source, value['codes'])
        if 'start' in value and 'end' in value:
            request = self.prepare_request(source, value)
            coverage = self.coverage(source, request)
            if coverage is None:
                raise ValueError('coverage is not registered for this source')
            result['coverage'] = dict(coverage)
            if source.get('method') == 'daily':
                complete = self.calendar_coverage(source, value['start'], value['end'])
                result['coverage']['calendar_evidence_complete'] = complete
                result['coverage']['complete'] = coverage['complete'] and complete
        return json.loads(json.dumps(result, allow_nan=False))


    def last_date(self, source: Mapping, codes) -> str | None:
        """Read the watermark for these securities, never an unrelated index."""
        if source.get('method') == 'daily':
            import duckdb
            from copy import deepcopy
            descriptor = deepcopy(dict(source))
            descriptor['defaults'] = {k: v for k, v in source.get('defaults', {}).items() if k != 'codes'}
            with self.data.transaction('sources'):
                _, _, _, _, files, _, _ = self._daily_view_context(descriptor, source['dataset_id'])
                paths = [str(self.data.path('sources', identity, name)) for identity, name in files]
                aliases = sorted({v for code in codes for v in
                                  (code, normalize_security_id(code), normalize_security_id(code).split('.')[-1])})
                with duckdb.connect() as connection:
                    row = connection.execute(
                        'SELECT max(date) FROM read_parquet(?, union_by_name=true) WHERE code IN (SELECT unnest(?))',
                        [paths, aliases]).fetchone()
                return str(row[0]) if row and row[0] is not None else None
        dates = [row['date'] for row in self.record_rows(source)
                 if row.get('code') in codes and row.get('date')]
        return max(dates) if dates else None


    def calendar_coverage(self, source, start, end):
        """Return whether registered calendar facts cover the requested range."""
        dataset = source['dataset_id']
        manifest = self.data.manifest(dataset)
        name = manifest.get('inputs', {}).get('calendar')
        if not isinstance(name, str):
            return False
        parts = Path(name).parts
        payload = (self.data.read('sources', parts[1], Path(*parts[2:]).as_posix())
                   if parts[0] == 'source' else self.data.read('sources', dataset, name))
        _, coverage = _calendar_facts(json.loads(payload))
        start_date = fact_date(start, 'calendar query start')
        end_date = fact_date(end, 'calendar query end')
        if start_date > end_date:
            raise ValueError('calendar query range is reversed')
        return _calendar_covers(coverage, start_date, end_date)


    def record_rows(self, source):
        with self.data.transaction('sources'):
            offset = 0
            while True:
                view = self.records(source, offset, 1000)
                if view is None:
                    return
                yield from view['records']
                offset += len(view['records'])
                if not view['records'] or offset >= view['total']:
                    return



class CollectionError(RuntimeError):
    def __init__(self, message, evidence=()):
        super().__init__(message)
        self.evidence = list(evidence)


def http(url, data=None):
    request = URLRequest(url, data=data, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.cninfo.com.cn/",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return json.loads(payload)


class EastmoneyQuotes:
    keys = ('code', 'data_at')
    def __init__(self, config, transport: Callable = http):
        self.config, self.transport = config, transport

    def collect(self, request):
        raw = []
        try:
            names = request.get("fields")
            allowed = {"name", "price", "change_pct", "volume", "amount"}
            if names is not None and (not names or not set(names) <= allowed
                                      or len(names) != len(set(names))):
                raise ValueError("invalid quote fields")
            if request.get("frequency", "d") != "d" or request.get("adjustment", "3") != "3":
                raise ValueError("latest quotes do not support historical frequency or adjustment")
            codes = request["codes"]
            if not codes or len(codes) != len(set(codes)) \
                    or any(not re.fullmatch(r"[01]\.\d{6}", code) for code in codes):
                raise ValueError("quotes codes require market.number e.g. 1.600000")
            fields = "f2,f3,f4,f5,f6,f12,f13,f14,f124"
            url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + urlencode(
                {"secids": ",".join(codes), "fields": fields, "fltt": "2"})
            response = self.transport(url)
            raw.append({"url": url, "request": request, "response": response})
            rows = response.get("data", {}).get("diff")
            if not isinstance(rows, list) or len(rows) != len(codes):
                raise ValueError("quote response missing requested securities")
            records = []
            for row in rows:
                code = f"{row['f13']}.{row['f12']}"
                if code not in codes or type(row.get("f124")) is not int or row["f124"] <= 0:
                    raise ValueError("invalid quote identity or timestamp")
                for field in ("f2", "f3", "f5", "f6"):
                    if type(row.get(field)) not in (int, float) or not math.isfinite(row[field]):
                        raise ValueError("invalid quote value")
                if any(row[field] < 0 for field in ("f2", "f5", "f6")):
                    raise ValueError("negative quote value")
                records.append({"code": code, "name": row["f14"], "price": row["f2"],
                                "change_pct": row["f3"], "volume": row["f5"], "amount": row["f6"],
                                "data_at": datetime.fromtimestamp(
                                    row["f124"], timezone.utc).isoformat(), "raw_page": 0})
            if len({row["code"] for row in records}) != len(codes):
                raise ValueError("duplicate quotes")
            if names is not None:
                records = [{key: value for key, value in row.items()
                            if key in {"code", "data_at", "raw_page"} | set(names)}
                            for row in records]
            return {"records": records, "raw": raw,
                    "data_as_of": max(row["data_at"] for row in records),
                    "scope": {"codes": codes, "frequency": "provider quote snapshot"}}
        except Exception as error:
            raise CollectionError(str(error), raw) from error


class BaostockDaily:
    _numeric_fields = frozenset({
        "open", "high", "low", "close", "volume", "amount", "turn",
        "pctChg", "tradestatus", "isST", "peTTM", "pbMRQ", "psTTM",
        "pcfNcfTTM", "adjustflag",
    })
    _required_daily_fields = frozenset({
        "open", "high", "low", "close", "volume", "amount",
    })

    def __init__(self, config):
        self.config = config

    @staticmethod
    def _validate_request(request):
        from protocol import normalize_security_id
        codes = request.get("codes")
        try:
            codes = [normalize_security_id(code) for code in codes]
        except (TypeError, ValueError) as error:
            raise ValueError("daily codes require a public security identity") from error
        if not codes or len(codes) != len(set(codes)):
            raise ValueError("daily codes require sh.number or sz.number")
        frequency = request.get("frequency", "d")
        adjustment = request.get("adjustment", "3")
        if frequency not in {"d", "w", "m", "5", "15", "30", "60"} \
                or adjustment not in {"1", "2", "3"}:
            raise ValueError("invalid frequency or adjustment")
        calendar_end = request.get("calendar_end")
        if calendar_end is not None:
            if not isinstance(calendar_end, str):
                raise ValueError("calendar_end must be an ISO date")
            try:
                calendar_end_day = date.fromisoformat(calendar_end)
                request_end = date.fromisoformat(request["end"])
            except (TypeError, ValueError) as error:
                raise ValueError("calendar_end must be an ISO date") from error
            if calendar_end_day < request_end:
                raise ValueError("calendar_end must include the request end")
        names = request.get("fields")
        if names is None:
            names = ["open", "high", "low", "close", "volume", "amount", "adjustflag"]
            if frequency not in {"5", "15", "30", "60"}:
                names.append("pctChg")
        if not isinstance(names, (list, tuple)):
            raise ValueError("invalid daily fields")
        names = list(dict.fromkeys(
            ["date"] + (["time"] if frequency in {"5", "15", "30", "60"} else [])
            + ["code"] + list(names)))
        allowed = {"date", "time", "code", "open", "high", "low", "close",
                   "volume", "amount", "adjustflag", "turn", "tradestatus",
                   "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST"}
        if not isinstance(names, (list, tuple)) or not {"date", "code"} <= set(names) \
                or not set(names) <= allowed or len(names) != len(set(names)):
            raise ValueError("invalid daily fields")
        if frequency in {"5", "15", "30", "60"} and "time" not in names:
            raise ValueError("minute bars require time")
        return list(codes), frequency, adjustment, list(names)

    @staticmethod
    def trade_calendar(bs, start: str, end: str):
        try:
            start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError) as error:
            raise ValueError("calendar dates must be ISO dates") from error
        if start_day > end_day:
            raise ValueError("calendar start must not exceed end")
        response = bs.query_trade_dates(start_date=start, end_date=end)
        if str(getattr(response, "error_code", "")) != "0":
            raise ProviderError(
                f"trade calendar failed: {getattr(response, 'error_code', '')} "
                f"{getattr(response, 'error_msg', '')}",
                str(getattr(response, "error_code", "")),
            )
        fields = list(getattr(response, "fields", ()))
        required = {"calendar_date", "is_trading_day"}
        if len(fields) != len(set(fields)) or not required <= set(fields):
            raise ValueError("trade calendar fields are incomplete")
        rows = []
        while response.next():
            rows.append(list(response.get_row_data()))
        if any(len(row) != len(fields) for row in rows):
            raise ProviderError("trade calendar row length differs from fields")
        dates, all_days = [], []
        for row in rows:
            item = dict(zip(fields, row))
            try:
                raw_day = item["calendar_date"]
                day = date.fromisoformat(str(raw_day))
            except (TypeError, ValueError) as error:
                raise ValueError("trade calendar date is invalid") from error
            if day.isoformat() != raw_day or not start_day <= day <= end_day:
                raise ValueError("trade calendar response is outside request")
            flag = str(item["is_trading_day"])
            if flag not in {"0", "1"} or day in all_days:
                raise ValueError("trade calendar response has invalid or duplicate dates")
            all_days.append(day)
            if flag == "1":
                dates.append(day)
        expected = [start_day + timedelta(days=offset)
                    for offset in range((end_day - start_day).days + 1)]
        if sorted(all_days) != expected:
            raise ValueError("trade calendar response does not cover the full request")
        dates.sort()
        return dates, {"provider": "baostock", "request": {
            "start_date": start, "end_date": end,
        }, "response": {"fields": fields, "rows": rows,
                         "error_code": str(response.error_code)}}

    def collect(self, request):
        import baostock as bs
        codes, frequency, adjustment, names = self._validate_request(request)
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"login {login.error_code}: {login.error_msg}")
        records, raw = [], []
        try:
            calendar = {"sessions": [], "coverage": []}
            if frequency == "d":
                calendar_end = request.get("calendar_end") or (date.fromisoformat(request['end']) + timedelta(days=31)).isoformat()
                all_sessions, calendar_raw = self.trade_calendar(
                    bs, request["start"], calendar_end)
                calendar = {"sessions": [day.isoformat() for day in all_sessions],
                            "coverage": [{"start": request["start"], "end": calendar_end}]}
                raw.append(calendar_raw)
            for code in codes:
                if code.split('.')[0] == 'bj':
                    packet = EastmoneyDaily(self.config).collect({**request,'codes':[code]})
                    offset = len(raw)
                    records.extend({**row,'raw_page':row['raw_page']+offset} for row in packet['records'])
                    raw.extend(packet['raw'])
                    continue
                fields = ",".join(names)
                response = bs.query_history_k_data_plus(
                    code, fields, start_date=request["start"], end_date=request["end"],
                    frequency=frequency, adjustflag=adjustment)
                if response.error_code != "0":
                    raise RuntimeError(response.error_msg)
                rows = []
                while response.next():
                    rows.append(response.get_row_data())
                if response.fields != fields.split(","):
                    raise ValueError("daily field contract changed")
                raw.append({"provider": "baostock", "request": {**request, "code": code,
                            "fields": fields, "adjustflag": adjustment},
                            "response": {"fields": response.fields, "rows": rows,
                                         "error_code": response.error_code}})
                for row in rows:
                    if len(row) != len(response.fields):
                        raise ValueError("daily row length mismatch")
                    item = dict(zip(response.fields, row))
                    if item["code"] != code or not request["start"] <= item["date"] <= request["end"]:
                        raise ValueError("daily request/response mismatch")
                    if item.get('tradestatus') == '0':
                        for name in ('volume','amount'):
                            if name in item and (item[name] is None or not str(item[name]).strip()):
                                item[name] = 0.0
                    for name in self._numeric_fields & item.keys():
                        raw_value = item[name]
                        if raw_value is None or (
                            isinstance(raw_value, str) and not raw_value.strip()
                        ):
                            if name in self._required_daily_fields:
                                raise ValueError(f"required daily field is empty: {name} ({code} {item['date']}, volume={item.get('volume')}, tradestatus={item.get('tradestatus')})")
                            item[name] = None
                            continue
                        try:
                            value = float(raw_value)
                        except (TypeError, ValueError) as error:
                            raise ValueError(f"daily field is invalid: {name}") from error
                        if not math.isfinite(value):
                            raise ValueError(f"daily field is invalid: {name}")
                        item[name] = value
                    item["raw_page"] = len(raw) - 1
                    records.append(item)
        except CollectionError as error:
            raise CollectionError(str(error), [*raw, *error.evidence]) from error
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        finally:
            bs.logout()
        if len({(row["code"], row["date"], row.get("time")) for row in records}) != len(records):
            raise ValueError("duplicate bar keys")
        return {"records": records, "raw": raw,
                "data_as_of": max((row["date"] for row in records), default=None),
                "frequency": frequency, "adjustment": adjustment,
                "fields": list(names),
                "calendar": calendar,
                "scope": {"codes": codes, "start": request["start"], "end": request["end"],
                          "frequency": request.get("frequency", "d"),
                          "adjustment": request.get("adjustment", "3")}}


def parse_cninfo_page(response, number, size):
    total, more, rows = response.get("totalRecordNum"), response.get("hasMore"), response.get("announcements")
    if rows is None and total == 0:
        rows = []
    if type(total) is not int or total < 0 or type(more) is not bool or not isinstance(rows, list):
        raise ValueError("invalid announcement response")
    if len(rows) != max(0, min(size, total - (number - 1) * size)) or more != (number * size < total):
        raise ValueError("announcement page disagrees with total")
    result = []
    for row in rows:
        required = ("announcementId", "orgId", "secCode", "announcementTitle", "adjunctUrl")
        if any(not isinstance(row.get(key), str) or not row[key] for key in required):
            raise ValueError("missing announcement field")
        if not isinstance(row.get("announcementTime"), int):
            raise ValueError("missing publication date")
        link = urljoin("https://static.cninfo.com.cn/", row["adjunctUrl"])
        if not link.startswith("https://static.cninfo.com.cn/"):
            raise ValueError("unexpected document host")
        result.append({"id": row["announcementId"], "org_id": row["orgId"],
                       "code": row["secCode"], "name": row.get("secName"),
                       "title": row["announcementTitle"],
                       "published_at": datetime.fromtimestamp(
                           row["announcementTime"] / 1000, timezone.utc).isoformat(),
                       "report_period": None, "url": link})
    return result, total, more


class CninfoAnnouncements:
    keys = ('id', 'org_id', 'code')
    def __init__(self, config, transport: Callable = http):
        self.config, self.transport = config, transport

    def collect(self, request):
        raw, records, expected, page = [], [], None, 1
        try:
            while True:
                query = {"pageNum": str(page), "pageSize": "30",
                         "seDate": request["start"] + "~" + request["end"],
                         "tabName": "fulltext", "column": "szse", "plate": "",
                         "stock": "", "searchkey": request.get("keyword", ""),
                         "category": self.config.get("category", ""), "isHLtitle": "true"}
                url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
                response = self.transport(url, urlencode(query).encode())
                raw.append({"url": url, "request": query, "response": response})
                rows, total, more = parse_cninfo_page(response, page, 30)
                if expected is not None and total != expected:
                    raise ValueError("announcement total changed during pagination")
                expected = total
                for row in rows:
                    key = (row["id"], row["org_id"], row["code"])
                    if any((item["id"], item["org_id"], item["code"]) == key for item in records):
                        raise ValueError("duplicate announcement identity")
                    row["raw_page"] = len(raw) - 1
                    records.append(row)
                if not more:
                    break
                page += 1
            return {"records": records, "raw": raw,
                    "data_as_of": max((row["published_at"] for row in records), default=None),
                    "scope": {"start": request["start"], "end": request["end"],
                              "keyword": request.get("keyword", ""),
                              "category": self.config.get("category", ""),
                              "pages": len(raw), "total": expected}}
        except Exception as error:
            raise CollectionError(str(error), raw) from error


class Securities:
    keys = ('exchange', 'code')
    """Collect official delisting records without owning persistence."""

    def __init__(self, config, provider=None):
        self.config = config
        self.provider = provider

    def collect(self, request):
        import re
        from protocol import normalize_security_id
        raw, records = [], []
        provider = self.provider
        if provider is None:
            import akshare as provider
        tables = (
            ("SH", provider.stock_info_sh_delist, "全部",
             {"公司代码": "code", "公司简称": "name", "上市日期": "list_date", "暂停上市日期": "delist_date"}),
            ("SZ", provider.stock_info_sz_delist, "终止上市公司",
             {"证券代码": "code", "证券简称": "name", "上市日期": "list_date", "终止上市日期": "delist_date"}),
        )
        requested = {normalize_security_id(code) for code in request.get("codes", ())}
        try:
            for exchange, query, symbol, names in tables:
                frame = query(symbol=symbol)
                rows = json.loads(frame.to_json(orient="records", force_ascii=False,
                                                date_format="iso"))
                raw.append({"provider": "exchange", "request": {"exchange": exchange,
                            "symbol": symbol}, "response": rows})
                for row in rows:
                    value = {name: row[column] for column, name in names.items()}
                    code = value["code"]
                    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                        raise ValueError("invalid official security identity")
                    bs_code = normalize_security_id(f"{exchange.lower()}.{code}")
                    value.update(exchange=exchange, bs_code=bs_code,
                                 raw_page=len(raw) - 1)
                    if not request["start"] <= value["delist_date"] <= request["end"]:
                        continue
                    if requested and value["bs_code"] not in requested:
                        continue
                    records.append(value)
            keys = [(row["exchange"], row["code"]) for row in records]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate official securities")
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {"records": records, "raw": raw, "data_as_of": request["end"],
                "scope": {"start": request["start"], "end": request["end"],
                          "codes": request.get("codes", [])}}


class News:
    keys = ('url',)
    date_field = 'date'

    @staticmethod
    def validate_record(row):
        day = date.fromisoformat(row['date'])
        if row.get('url') != f'https://cn.govopendata.com/xinwenlianbo/{day:%Y%m%d}/':
            raise ValueError('invalid broadcast URL')
        articles = row.get('articles')
        if not isinstance(articles, list) or not articles:
            raise ValueError('broadcast articles are required')
        for seq, article in enumerate(articles, 1):
            if (not isinstance(article, Mapping) or article.get('seq') != seq
                    or any(not isinstance(article.get(key), str) or not article[key].strip()
                           for key in ('headline', 'content'))):
                raise ValueError('incomplete broadcast article')
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or self._http

    def _http(self, url):
        request = URLRequest(url, headers={"User-Agent": "Lianghua/1.0"})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8")

    def collect(self, request):
        from bs4 import BeautifulSoup
        start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
        if start > end:
            raise ValueError("start must not exceed end")
        interval = request.get('interval', self.config.get('params', {}).get('interval', 1))
        self.timeout = request.get('timeout', self.config.get('params', {}).get('timeout', 30))
        for name, value in (('interval', interval), ('timeout', self.timeout)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or (name == 'timeout' and value == 0)):
                raise ValueError(f'invalid {name}')
        completed = set(request.get('completed_dates', ()))
        records, raw, day = [], [], start
        try:
            while day <= end:
                if day.isoformat() in completed:
                    day += timedelta(days=1)
                    continue
                if raw:
                    time.sleep(max(1, interval))
                url = f"https://cn.govopendata.com/xinwenlianbo/{day:%Y%m%d}/"
                page = self.transport(url)
                raw.append({"url": url, "request": {"date": day.isoformat()}, "response": page})
                soup = BeautifulSoup(page, "html.parser")
                title = soup.select_one('h1.article-title')
                match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', title.get_text() if title else '')
                if match is None or date(*(int(x) for x in match.groups())) != day:
                    raise ValueError('broadcast date does not match request')
                articles = soup.select("article.content-section")
                if not articles:
                    raise ValueError("broadcast page contains no articles")
                entries = []
                for seq, article in enumerate(articles, 1):
                    heading = article.find(["h1", "h2", "h3"])
                    body = article.select_one(".content-body")
                    if heading is None or body is None:
                        raise ValueError("incomplete broadcast article")
                    headline = heading.get_text(strip=True)
                    content = "\n".join(p.get_text().strip() for p in body.find_all("p") if p.get_text().strip())
                    if not headline or not content:
                        raise ValueError("empty broadcast article")
                    entries.append({'seq': seq, 'section_id': article.get('id') or heading.get('id'),
                                    'headline': headline, 'content': content})
                links = soup.select('nav a[href^="#"], .toc a[href^="#"]')
                if links:
                    ids = [entry['section_id'] for entry in entries]
                    if (len(set(ids)) != len(ids) or any(value is None for value in ids)
                            or [link['href'][1:] for link in links] != ids):
                        raise ValueError('broadcast directory does not match articles')
                records.append({'date': day.isoformat(), 'url': url, 'articles': entries,
                                'raw_page': len(raw) - 1})
                day += timedelta(days=1)
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {"records": records, "raw": raw, "data_as_of": end.isoformat(),
                "scope": {"start": start.isoformat(), "end": end.isoformat(),
                          "keyword": request.get("keyword", "")}}


def collect_daily(request):
    return BaostockDaily({}).collect(request)


class BseCatalogue:
    """BSE's complete, paginated current security catalogue."""
    keys = ('code',)

    def __init__(self, config, transport=None):
        self.transport = self.page if transport is None else transport

    @staticmethod
    def page(number):
        url = 'https://www.bse.cn/nqxxController/nqxxCnzq.do'
        params = {'page': str(number), 'typejb': 'T', 'xxfcbj[]': '2',
                  'xxzqdm': '', 'sortfield': 'xxzqdm', 'sorttype': 'asc'}
        import requests
        response = requests.post(url, data=params, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bse.cn/nq/listedcompany.html'},
            timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        text = response.text
        payload, _ = json.JSONDecoder().raw_decode(text[text.index('['):])
        if not isinstance(payload, list) or len(payload) != 1:
            raise ProviderError('invalid BSE catalogue response')
        return payload[0]

    def collect(self, request):
        records, raw = [], []
        try:
            first = self.transport(0)
            pages, total = first['totalPages'], first['totalElements']
            if type(pages) is not int or type(total) is not int or not 0 < pages <= total:
                raise ProviderError('invalid BSE catalogue totals')
            for number in range(pages):
                page = first if number == 0 else self.transport(number)
                raw.append({'provider':'bse','request':{'page':number},'response':page})
                rows = page['content']
                if (page['number'] != number or page['totalPages'] != pages
                        or page['totalElements'] != total or not isinstance(rows, list)
                        or not rows or page['numberOfElements'] != len(rows)):
                    raise ProviderError('BSE catalogue pagination changed')
                for row in rows:
                    code = row['xxzqdm']
                    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
                        raise ProviderError('invalid BSE security code')
                    opened = datetime.strptime(row['fxssrq'], '%Y%m%d').date().isoformat()
                    records.append({'code':'bj.'+code,'code_name':row['xxzqjc'],
                                    'ipoDate':opened,'outDate':'','type':'1','status':'1',
                                    'raw_page':len(raw)-1})
            if len(records) != total or len({r['code'] for r in records}) != total:
                raise ProviderError('BSE catalogue count or identities disagree')
            return {'records':records,'raw':raw,'scope':{},
                    'data_as_of':datetime.now(timezone.utc).isoformat()}
        except Exception as error:
            raise CollectionError(str(error),raw) from error


class EastmoneyFactors:
    """Historical standard valuations from Eastmoney, including net-cash TTM."""
    fields = {'peTTM':'PE_TTM','pbMRQ':'PB_MRQ','psTTM':'PS_TTM'}

    def __init__(self, config, transport=None):
        self.transport = EastmoneyDaily.get if transport is None else transport

    @staticmethod
    def number(value):
        if isinstance(value, bool):
            raise ProviderError('invalid Eastmoney numeric value')
        result = float(value)
        if not math.isfinite(result):
            raise ProviderError('non-finite Eastmoney value')
        return result

    def pages(self, report, code, sort, raw, clause=''):
        exchange, number = code.split('.')
        params = {'reportName':report,'columns':'ALL','pageNumber':'1','pageSize':'500',
                  'sortColumns':sort,'sortTypes':'-1','source':'WEB','client':'WEB',
                  'filter':f'(SECUCODE="{number}.{exchange.upper()}")'+clause}
        records, total, pages = [], None, None
        page = 1
        while pages is None or page <= pages:
            params['pageNumber'] = str(page)
            url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?'+urlencode(params)
            response = self.transport(url)
            raw.append({'provider':'eastmoney','request':{'url':url},'response':response})
            result = response.get('result')
            if response.get('success') is not True or not isinstance(result, dict):
                raise ProviderError('Eastmoney report failed: '+str(response.get('message')))
            if total is None:
                total, pages = result['count'], result['pages']
                if type(total) is not int or type(pages) is not int or not 0 < pages <= total:
                    raise ProviderError('Eastmoney report has no complete records')
            if result['count'] != total or result['pages'] != pages:
                raise ProviderError('Eastmoney report totals changed')
            rows = result['data']
            if not isinstance(rows, list) or not rows:
                raise ProviderError('Eastmoney report page is empty')
            for row in rows:
                if row.get('SECUCODE') != f'{number}.{exchange.upper()}' or row.get('SECURITY_CODE') != number:
                    raise ProviderError('Eastmoney report security identity disagrees')
                records.append({**row,'_raw_page':len(raw)-1})
            page += 1
        if len(records) != total:
            raise ProviderError('Eastmoney report count disagrees')
        return records

    @classmethod
    def cash_ttm(cls, reports, day):
        known = {}
        for row in reports:
            period = date.fromisoformat(row['REPORT_DATE'][:10]).isoformat()
            notice = date.fromisoformat(row['NOTICE_DATE'][:10]).isoformat()
            # Date-only notices do not establish availability before that day's close.
            if notice >= day:
                continue
            if period > notice:
                raise ProviderError('cash report period is after disclosure')
            if period not in known or known[period]['NOTICE_DATE'] < row['NOTICE_DATE']:
                known[period] = row
        if not known:
            raise ProviderError('no disclosed cash report for '+day)
        period = max(known)
        value = cls.number(known[period]['CCE_ADD'])
        if period.endswith('-12-31'):
            return value
        year = int(period[:4])-1
        annual, prior = f'{year}-12-31', f'{year}{period[4:]}'
        if annual not in known or prior not in known:
            raise ProviderError('net cash TTM lacks disclosed prior-year reports for '+day)
        return value+cls.number(known[annual]['CCE_ADD'])-cls.number(known[prior]['CCE_ADD'])

    def collect(self, request):
        fields = set(request['fields'])
        if fields - set(self.fields) - {'pcfNcfTTM','date','code'}:
            raise ValueError('unsupported Eastmoney factor fields')
        raw, records = [], []
        try:
            for code in request['codes']:
                clause = f"(TRADE_DATE>='{request['start']}')(TRADE_DATE<='{request['end']}')"
                values = self.pages('RPT_VALUEANALYSIS_DET',code,'TRADE_DATE',raw,clause)
                reports = self.pages('RPT_DMSK_FN_CASHFLOW',code,'REPORT_DATE',raw) if 'pcfNcfTTM' in fields else []
                seen = set()
                for value in values:
                    day = date.fromisoformat(value['TRADE_DATE'][:10]).isoformat()
                    if day in seen or not request['start'] <= day <= request['end']:
                        raise ProviderError('duplicate or out-of-range Eastmoney factor date')
                    seen.add(day)
                    row = {'code':code,'date':day,'raw_page':value['_raw_page']}
                    for field, source in self.fields.items():
                        if field in fields:
                            row[field] = None if value[source] is None else self.number(value[source])
                    if 'pcfNcfTTM' in fields:
                        cash = self.cash_ttm(reports,day)
                        market_cap = self.number(value['TOTAL_MARKET_CAP'])
                        if market_cap <= 0:
                            raise ProviderError('invalid total market value')
                        row['pcfNcfTTM'] = market_cap/cash if cash != 0 else None
                    records.append(row)
            return {'records':records,'raw':raw,'scope':{'codes':request['codes'],
                    'start':request['start'],'end':request['end']}}
        except Exception as error:
            raise CollectionError(str(error),raw) from error


class EastmoneyDaily:
    """Eastmoney OHLCV; provider lots become standard shares."""
    fields = ('date','open','close','high','low','volume','amount','amplitude','pctChg','change','turn')

    def __init__(self, config, transport=None):
        self.transport = self.get if transport is None else transport

    @staticmethod
    def get(url):
        request = URLRequest(url, headers={'User-Agent':'Mozilla/5.0',
                                          'Referer':'https://quote.eastmoney.com/'})
        with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            return json.loads(response.read())

    def collect(self, request):
        codes, frequency, adjustment, names = BaostockDaily._validate_request(request)
        factor_fields = set(names) & (set(EastmoneyFactors.fields) | {'pcfNcfTTM'})
        unsupported = set(names) - set(self.fields) - factor_fields - {'code','adjustflag','tradestatus'}
        if unsupported or frequency not in {'d','w','m'}:
            raise ValueError('Eastmoney historical fields/frequency unavailable: '+','.join(sorted(unsupported or {frequency})))
        periods = {'d':'101','w':'102','m':'103'}
        # Provider adjustment flags differ from the public Baostock contract.
        adjustments = {'1':'2','2':'1','3':'0'}
        records, raw = [], []
        try:
            for code in codes:
                exchange, number = code.split('.')
                market = {'sh':'1','sz':'0','bj':'0'}[exchange]
                params = {'fields1':'f1,f2,f3,f4,f5,f6',
                          'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                          'secid':market+'.'+number,'klt':periods[frequency],
                          'fqt':adjustments[adjustment],
                          'beg':request['start'].replace('-',''), 'end':request['end'].replace('-','')}
                url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?'+urlencode(params)
                response = self.transport(url)
                raw.append({'provider':'eastmoney','request':{'url':url},'response':response})
                data = response.get('data')
                if response.get('rc') != 0 or not isinstance(data, dict) or data.get('code') != number:
                    raise ProviderError('Eastmoney daily response identity or status disagrees')
                rows = data.get('klines')
                if not isinstance(rows, list):
                    raise ProviderError('Eastmoney daily bars are missing')
                for line in rows:
                    cells = line.split(',')
                    if len(cells) != len(self.fields):
                        raise ProviderError('Eastmoney daily field contract changed')
                    row = dict(zip(self.fields, cells))
                    day = date.fromisoformat(row['date']).isoformat()
                    if not request['start'] <= day <= request['end']:
                        raise ProviderError('Eastmoney daily date outside request')
                    for field in self.fields[1:]:
                        row[field] = float(row[field])
                        if not math.isfinite(row[field]):
                            raise ProviderError('Eastmoney non-finite daily field: '+field)
                    if row['volume'] < 0 or row['amount'] < 0 or min(row[f] for f in ('open','high','low','close')) <= 0:
                        raise ProviderError('Eastmoney invalid daily prices or volume')
                    if 'tradestatus' in names:
                        if row['volume'] <= 0:
                            raise ProviderError('zero-volume bar lacks trading-status evidence')
                        row['tradestatus'] = 1.0
                    row['volume'] *= 100
                    row.update(code=code,adjustflag=float(adjustment))
                    records.append({**{field:row[field] for field in names if field not in factor_fields},'raw_page':len(raw)-1})
            if factor_fields:
                packet = EastmoneyFactors({},transport=self.transport).collect({**request,'fields':sorted(factor_fields)})
                raw.extend(packet['raw'])
                factors = {(r['code'],r['date']):r for r in packet['records']}
                for row in records:
                    key = row['code'],row['date']
                    if key not in factors:
                        raise ProviderError('missing matching Eastmoney factor date: '+str(key))
                    row.update({field:factors[key][field] for field in factor_fields})
            if len({(r['code'],r['date']) for r in records}) != len(records):
                raise ProviderError('Eastmoney duplicate daily bars')
            return {'records':records,'raw':raw,'scope':{'codes':codes,'start':request['start'],'end':request['end']},
                    'data_as_of':max((r['date'] for r in records),default=None)}
        except CollectionError as error:
            raise CollectionError(str(error),[*raw,*error.evidence]) from error
        except Exception as error:
            raise CollectionError(str(error),raw) from error


class Catalogue:
    """Provider security catalogue; publication uses the usual method packet."""
    keys = ('code',)

    def __init__(self, config):
        self.config = config

    def collect(self, request):
        import baostock as bs
        exchanges = request.get('exchanges', ['sh','sz','bj'])
        if not isinstance(exchanges, (list, tuple)) or not exchanges or set(exchanges) - {'sh','sz','bj'}:
            raise ValueError('unsupported catalogue exchanges')
        login = bs.login()
        if str(login.error_code) != '0':
            raise ProviderError('security catalogue login failed: ' + str(login.error_code))
        raw = []
        try:
            with _timeout(DEFAULT_TIMEOUT):
                response = bs.query_stock_basic()
            if str(response.error_code) != '0':
                raise ProviderError('security catalogue failed: ' + str(response.error_msg))
            fields, rows = _response_rows(response, DEFAULT_TIMEOUT)
            _validate_stock_basic_rows(fields, rows)
            if not rows:
                raise ProviderError('security catalogue is empty')
            raw.append({'provider': 'baostock', 'request': {'method':'query_stock_basic'},
                        'response': {'fields':fields,'rows':rows,'error_code':str(response.error_code)}})
            records = [{**dict(zip(fields, row)), 'raw_page':0} for row in rows
                       if row[fields.index('code')].split('.')[0] in exchanges]
            if 'bj' in exchanges:
                bse = BseCatalogue({}).collect({})
                records.extend({**row,'raw_page':row['raw_page']+len(raw)} for row in bse['records'])
                raw.extend(bse['raw'])
            return {'records':records, 'raw':raw, 'scope':{'exchanges':list(exchanges)},
                    'data_as_of':datetime.now(timezone.utc).isoformat()}
        except CollectionError as error:
            raise CollectionError(str(error), [*raw, *error.evidence]) from error
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        finally:
            bs.logout()


DEFAULT_METHODS = {
    "catalogue": Catalogue,
    "news": News,
    "quotes": EastmoneyQuotes,
    "daily": BaostockDaily,
    "announcements": CninfoAnnouncements,
    "securities": Securities,
}


class StockBasicEvidenceError(ValueError):
    pass


_BASIC_FIELDS = {"code", "ipoDate", "outDate", "type", "status"}


def _validate_stock_basic_rows(fields, rows):
    if len(fields) != len(set(fields)) or not _BASIC_FIELDS <= set(fields):
        raise StockBasicEvidenceError("stock-basic fields are missing or duplicated")
    positions = {field: fields.index(field) for field in _BASIC_FIELDS}
    seen = set()
    for row in rows:
        if len(row) != len(fields):
            raise StockBasicEvidenceError("stock-basic row length differs from fields")
        code = row[positions["code"]]
        if not isinstance(code, str) or not code or code in seen:
            raise StockBasicEvidenceError(f"duplicate or missing stock-basic code: {code!r}")
        seen.add(code)
        if row[positions["type"]] != "1":
            continue
        try:
            date.fromisoformat(row[positions["ipoDate"]])
            if row[positions["outDate"]]:
                date.fromisoformat(row[positions["outDate"]])
        except (TypeError, ValueError) as error:
            raise StockBasicEvidenceError(f"invalid IPO or delisting date for {code}") from error


def collect_stock_basic(provider, *, timeout: int, code: str) -> dict:
    requested = _bs_code(code)
    with _timeout(timeout):
        response = provider.query_stock_basic(code=requested)
    if str(getattr(response, "error_code", "")) != "0":
        raise ProviderError(f"stock-basic query failed: {getattr(response, 'error_code', '')} "
                            f"{getattr(response, 'error_msg', '')}",
                            str(getattr(response, "error_code", "")))
    fields, rows = _response_rows(response, timeout)
    _validate_stock_basic_rows(fields, rows)
    if len(rows) != 1 or rows[0][fields.index("code")] != requested:
        raise StockBasicEvidenceError(f"stock-basic response does not match {requested}")
    raw = {"error_code": str(response.error_code), "error_msg": str(response.error_msg),
           "fields": fields, "rows": rows}
    return {"schema": 1, "request": f"query_stock_basic({requested})",
            "raw_response": raw, "raw_response_sha256": _sha256(raw),
            "collected_at": datetime.now(timezone.utc).isoformat()}


def read_stock_basic(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise StockBasicEvidenceError("unsupported stock-basic snapshot schema")
    request = payload.get("request", "")
    if not isinstance(request, str) or not request.startswith("query_stock_basic(") or not request.endswith(")"):
        raise StockBasicEvidenceError("stock-basic request mismatch")
    requested = request[len("query_stock_basic("):-1]
    try:
        if _bs_code(requested) != requested:
            raise StockBasicEvidenceError("stock-basic requested code is not canonical")
    except ValueError as error:
        raise StockBasicEvidenceError("stock-basic requested code is invalid") from error
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256(raw):
        raise StockBasicEvidenceError("stock-basic response hash mismatch")
    if raw.get("error_code") != "0":
        raise StockBasicEvidenceError("stock-basic source recorded a provider error")
    fields, rows = raw.get("fields"), raw.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise StockBasicEvidenceError("stock-basic rows or fields malformed")
    _validate_stock_basic_rows(fields, rows)
    if len(rows) != 1 or rows[0][fields.index("code")] != requested:
        raise StockBasicEvidenceError("stock-basic response does not match requested code")
    return payload


def write_stock_basic(data, payload: dict):
    read_stock_basic(payload)
    facts = normalize_facts(payload)
    validate_facts(facts, kind=facts["kind"], code=facts["code"],
                   start=facts["start"], end=facts["end"], year=facts["year"])
    data.write("state", "collect", f"{uuid4().hex}.json",
               (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode())
    value = (json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return data.write("sources", "facts", f"stock_basic_{facts['code']}.json", value)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _optional(data, identity, name):
    from protocol import DataError
    try:
        return data.read("sources", identity, name)
    except DataError as error:
        if error.code == "missing":
            return None
        raise


def fetch_attachment(url):
    import requests
    response = requests.get(url, headers={"Referer": "https://www.cninfo.com.cn/"},
                            timeout=20, allow_redirects=False)
    return response.status_code, dict(response.headers), response.content


def capture_attachments(plan, data, identity="announcement_documents", *, fetcher=fetch_attachment):
    if plan.get("origin") != "https://static.cninfo.com.cn/":
        raise ValueError("official attachment origin required")
    paths = [entry["relative_path"] for entry in plan.get("entries", [])]
    if not paths or paths != sorted(set(paths)):
        raise ValueError("nonempty unique sorted attachment paths required")
    for path in paths:
        if (not isinstance(path, str) or not path.startswith("finalpage/") or
                ".." in path.split("/") or any(char in path for char in ("?", "#", "\\"))):
            raise ValueError("invalid attachment path")
    report = {"schema": 1, "plan_sha256": _sha(_bytes(plan)),
              "index_complete": plan["index_complete"], "attempted": 0,
              "failures": 0, "transport_complete": False,
              "release_status": "NOT_RELEASED", "sources": []}
    previous_bytes = _optional(data, identity, "manifest.json")
    if previous_bytes is not None:
        previous = json.loads(previous_bytes)
        if previous["plan_sha256"] != report["plan_sha256"]:
            raise ValueError("capture plan changed; separate evidence directory required")
        for source in previous["sources"]:
            receipt_bytes = data.read("sources", identity, source["receipt_file"])
            if _sha(receipt_bytes) != source["receipt_sha256"]:
                raise ValueError("receipt hash mismatch")
            receipt = json.loads(receipt_bytes)
            if "raw_sha256" in receipt and _sha(data.read("sources", identity, receipt["raw_file"])) != receipt["raw_sha256"]:
                raise ValueError("raw hash mismatch")
    try:
        for path in paths:
            url = plan["origin"] + path
            request = {"url": url, "method": "GET",
                       "request_headers": {"Referer": "https://www.cninfo.com.cn/"}}
            key = _sha(_bytes(request)); receipt_name, raw_name = key + ".json", key + ".raw"
            receipt_bytes = _optional(data, identity, receipt_name)
            if receipt_bytes is not None:
                receipt = json.loads(receipt_bytes)
                if receipt["request"] != request:
                    raise ValueError("request identity mismatch")
                if "raw_sha256" in receipt and _sha(data.read("sources", identity, raw_name)) != receipt["raw_sha256"]:
                    raise ValueError("raw hash mismatch")
            else:
                if _optional(data, identity, raw_name) is not None:
                    raise ValueError("unreceipted raw evidence; no refresh")
                receipt = {"schema": 1, "request": request}
                try:
                    status, headers, raw = fetcher(url)
                except Exception as error:
                    receipt.update(error_type=type(error).__name__, error=str(error))
                else:
                    data.write("sources", identity, raw_name, raw)
                    receipt.update(http_status=status, response_headers=headers, raw_file=raw_name,
                                   raw_sha256=_sha(raw), raw_bytes=len(raw))
                data.write("sources", identity, receipt_name, _bytes(receipt) + b"\n")
            success = receipt.get("http_status") == 200
            report["attempted"] += 1; report["failures"] += int(not success)
            report["sources"].append({"receipt_file": receipt_name,
                                      "receipt_sha256": _sha(data.read("sources", identity, receipt_name)),
                                      "http_200": success})
        report["transport_complete"] = report["failures"] == 0
    finally:
        report["run_id"] = _sha(_bytes(report))
        data.write("sources", identity, "manifest.json", _bytes(report) + b"\n")
    return report


def month_windows(period):
    from datetime import date, timedelta
    start, end = map(date.fromisoformat, period.split("~"))
    if start > end:
        raise ValueError("inverted announcement period")
    while start <= end:
        last = min(end, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]))
        yield f"{start.isoformat()}~{last.isoformat()}"
        start = last + timedelta(days=1)


def parse_page(raw, request):
    total, more, rows = raw.get("totalRecordNum"), raw.get("hasMore"), raw.get("announcements")
    if rows is None and total == 0: rows = []
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("invalid announcement total")
    if not isinstance(more, bool) or not isinstance(rows, list):
        raise ValueError("invalid announcement page shape")
    number, size = int(request["pageNum"]), int(request["pageSize"])
    expected = max(0, min(size, total - (number - 1) * size))
    if len(rows) != expected or more != (number * size < total):
        raise ValueError("page count or hasMore disagrees with total")
    keys = []
    for row in rows:
        key = tuple(row.get(field) for field in ("announcementId", "orgId", "secCode"))
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError("missing announcement identity")
        keys.append(key)
    return total, more, keys


def collect_announcements(template, data, identity="announcement_index", *, fetcher):
    if any(template.get(field, "") for field in ("stock", "secid", "column", "plate")):
        raise ValueError("unfiltered security/market query required")
    if int(template["pageSize"]) <= 0: raise ValueError("positive pageSize required")
    windows = list(month_windows(template["seDate"]))
    report = {"schema": 2, "query": template, "planned_windows": windows,
              "attempted_windows": 0, "windows": 0, "pages": 0, "records": 0,
              "failures": [], "sources": [], "complete": False, "release_status": "NOT_RELEASED"}
    seen, current = set(), None
    try:
        for window in windows:
            number, expected_total = 1, None; report["attempted_windows"] += 1
            try:
                while True:
                    current = dict(template, seDate=window, pageNum=str(number))
                    identity_value = {"url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                                      "headers": {"Referer": "https://www.cninfo.com.cn/"}, "request": current}
                    name = f"index_{_sha(_bytes(identity_value))}.json"
                    captured = _optional(data, identity, name)
                    if captured is None:
                        raw = fetcher(dict(current)).decode("utf-8")
                        payload = {"schema": 1, **identity_value, "request_sha256": _sha(_bytes(identity_value)),
                                   "raw_response": raw, "raw_response_sha256": _sha(raw.encode())}
                        data.write("sources", identity, name, _bytes(payload) + b"\n")
                    else: payload = json.loads(captured)
                    if payload.get("request_sha256") != _sha(_bytes(identity_value)):
                        raise ValueError("request hash mismatch")
                    raw = payload.get("raw_response")
                    if not isinstance(raw, str) or _sha(raw.encode()) != payload.get("raw_response_sha256"):
                        raise ValueError("response hash mismatch")
                    total, more, keys = parse_page(json.loads(raw), current)
                    if expected_total is not None and total != expected_total: raise ValueError("announcement total changed within window")
                    if any(key in seen for key in keys) or len(set(keys)) != len(keys): raise ValueError("duplicate announcement identity across capture")
                    expected_total = total; seen.update(keys); report["pages"] += 1; report["records"] += len(keys)
                    report["sources"].append({"file": name, "sha256": _sha(data.read("sources", identity, name)), "accepted": True})
                    if not more: break
                    number += 1
                report["windows"] += 1
            except Exception as error:
                report["failures"].append({"window": window, "request": current,
                                           "error_type": type(error).__name__, "error": str(error)})
        if report["failures"]: raise ValueError("; ".join(item["error"] for item in report["failures"]))
        report["complete"] = True
    finally:
        report["run_id"] = _sha(_bytes(report)); data.write("sources", identity, "manifest.json", _bytes(report) + b"\n")
    return report
