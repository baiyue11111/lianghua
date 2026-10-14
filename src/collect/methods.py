"""Fetch methods with standard DataBus delivery and process logging."""

from __future__ import annotations

import json
import argparse
import math
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
        if self.kind == "adjust":
            return f"adjust_{self.code}_{self.start}_{self.end}"
        return f"dividend_{self.code}_{self.year}"


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
    return data.write("sources", "research_cache", f"{request.key}.json",
                      (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def _validate_cache(request: Request, payload: dict) -> None:
    if payload.get("schema") != 2:
        raise CacheIntegrityError("unsupported or missing cache schema")
    expected = {"kind": request.kind, "code": _bs_code(request.code), "start": request.start,
                "end": request.end, "year": request.year}
    if payload.get("request") != expected or payload.get("request_sha256") != _sha256(expected):
        raise CacheIntegrityError("cached request mismatch")
    raw = payload.get("raw_response")
    if not isinstance(raw, dict) or payload.get("raw_response_sha256") != _sha256(raw):
        raise CacheIntegrityError("cached raw response hash mismatch")
    fields, rows = raw.get("fields"), raw.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list) or "code" not in fields:
        raise CacheIntegrityError("cached fields/rows are malformed")
    if str(raw.get("error_code")) != "0":
        raise CacheIntegrityError("cached raw response has provider error")
    index = fields.index("code")
    if any(not isinstance(row, list) or len(row) != len(fields) or row[index] != expected["code"] for row in rows):
        raise CacheIntegrityError("cached row identity or length mismatch")
    if payload.get("request_fields_sha256") != hashlib.sha256(",".join(fields).encode()).hexdigest():
        raise CacheIntegrityError("cached field hash mismatch")
    if payload.get("status") not in {"ok", "empty"} or (payload["status"] == "empty") != (not rows):
        raise CacheIntegrityError("cached status is invalid")


def pending_requests(requests: list[Request], data) -> list[Request]:
    from protocal import DataError
    pending = []
    for request in requests:
        try:
            payload = json.loads(data.read("sources", "research_cache", f"{request.key}.json"))
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


def _years(value: str) -> list[str]:
    if ":" in value:
        start, end = value.split(":", 1)
        if not (start.isdigit() and end.isdigit()) or int(start) > int(end):
            raise ValueError("year range must be START:END")
        return [str(year) for year in range(int(start), int(end) + 1)]
    if not value.isdigit():
        raise ValueError("year must be YYYY or YYYY:YYYY")
    return [value]


def build_requests(args) -> list[Request]:
    codes = [item.strip() for item in (args.codes or "").split(",") if item.strip()]
    unique = []
    seen = set()
    for code in codes:
        _bs_code(code)
        if code not in seen:
            seen.add(code)
            unique.append(code)
    if not unique:
        raise ValueError("--codes is required")
    if args.kind == "adjust":
        if not args.start or not args.end:
            raise ValueError("adjust requires --start and --end")
        return [Request("adjust", code, args.start, args.end, None) for code in unique]
    if not args.year:
        raise ValueError("dividend requires --year")
    return [Request("dividend", code, None, None, year)
            for code in unique for year in _years(args.year)]


def main(argv=None, *, data=None) -> int:
    parser = argparse.ArgumentParser(description="standard provider evidence collection")
    parser.add_argument("kind", choices=("adjust", "dividend"))
    parser.add_argument("--codes")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--year")
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if data is None:
        from protocal import build_data
        data = build_data(Path(__file__).resolve().parents[2])
    requests = build_requests(args)
    if not args.force:
        requests = limit_pending(requests, data, args.max_tasks)
    elif args.max_tasks:
        requests = requests[:args.max_tasks]
    if not requests:
        return 0
    import baostock as bs
    failed = 0
    try:
        login_with_timeout(bs, args.timeout)
        for request in requests:
            fetch_and_cache(request, bs, data, args.timeout)
    except Exception:
        failed = 1
    finally:
        try:
            with _timeout(args.timeout):
                bs.logout()
        except Exception:
            failed = 1
    return 1 if failed else 0


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
            if len(fields) == 1 and fields[0] in cls.FIELDSETS:
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
        for name in ("data_type", "storage_id", "dataset_id", "calendar"):
            if name in source and name not in result:
                result[name] = deepcopy(source[name])
        for name in ("frequency", "price_basis", "adjustment", "fields"):
            if name in frozen_request and name not in result:
                result[name] = deepcopy(frozen_request[name])
        if result.get("data_type") == "daily":
            packet_calendar = result.get("calendar")
            stored_name = source.get("calendar")
            if not isinstance(stored_name, str) and isinstance(packet_calendar, str):
                stored_name = packet_calendar
            stored = []
            if isinstance(stored_name, str):
                calendar_dataset = result.get("dataset_id") or source.get("dataset_id")
                payload = json.loads(self.data.read("sources", calendar_dataset, stored_name))
                stored = payload.get("sessions") if isinstance(payload, Mapping) else None
                if not isinstance(stored, list) or not stored:
                    raise ValueError("daily source calendar must contain sessions")
            actual = packet_calendar if isinstance(packet_calendar, list) else []
            sessions = sorted({str(value) for value in [*stored, *actual]})
            if not sessions and (stored_name is not None or packet_calendar is not None):
                raise ValueError("daily source calendar must contain sessions")
            if sessions:
                result["calendar"] = sessions
        return result

    def coverage(self, source: Mapping, request: Mapping):
        if source.get("data_type") != "daily" or request.get("force") is True:
            return None
        try:
            from protocal import DataError
            from datetime import date
            symbols = tuple(request.get("codes", ()))
            fields = tuple(request.get("fields") or source.get("fields") or
                           ("open", "high", "low", "close", "volume", "amount"))
            return self.data.daily_coverage(
                source.get("dataset_id", source["id"]), symbols,
                date.fromisoformat(request["start"]), date.fromisoformat(request["end"]), fields)
        except DataError as error:
            if error.code == "missing":
                return None
            raise

    @staticmethod
    def missing_request(request: Mapping, coverage: Mapping) -> dict:
        missing = coverage.get("missing", {})
        days = [day for values in missing.values() for day in values]
        codes = [code for code, values in missing.items() if values]
        if not days or not codes:
            return dict(request)
        value = dict(request)
        value["codes"] = codes
        value["start"], value["end"] = min(days), max(days)
        return value

    def publish(self, packet: Mapping):
        value = deepcopy(dict(packet))
        return self.data.publish_packet(value)

    def prepare_publish(self, packet: Mapping):
        """Validate the same merge candidate used by formal publication."""
        value = deepcopy(dict(packet))
        return self.data.prepare_packet(value)

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
        from protocal import normalize_security_id
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
        if not required <= set(fields):
            raise ValueError("trade calendar fields are incomplete")
        rows = []
        while response.next():
            rows.append(list(response.get_row_data()))
        if any(len(row) != len(fields) for row in rows):
            raise ProviderError("trade calendar row length differs from fields")
        dates = []
        for row in rows:
            item = dict(zip(fields, row))
            if str(item["is_trading_day"]) != "1":
                continue
            try:
                day = date.fromisoformat(str(item["calendar_date"]))
            except (TypeError, ValueError) as error:
                raise ValueError("trade calendar date is invalid") from error
            if not start_day <= day <= end_day:
                raise ValueError("trade calendar response is outside request")
            dates.append(day)
        dates = sorted(set(dates))
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
            calendar_sessions = []
            if frequency == "d":
                calendar_end = request.get("calendar_end") or date.today().isoformat()
                requested_end = date.fromisoformat(request["end"])
                all_sessions, calendar_raw = self.trade_calendar(
                    bs, request["start"], calendar_end)
                following = [day for day in all_sessions if day > requested_end]
                if not following:
                    raise ValueError("trade calendar has no following session")
                calendar_sessions = [day.isoformat() for day in all_sessions
                                     if day <= requested_end]
                calendar_sessions.append(following[0].isoformat())
                raw.append(calendar_raw)
            for code in codes:
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
                    for name in self._numeric_fields & item.keys():
                        raw_value = item[name]
                        if raw_value is None or (
                            isinstance(raw_value, str) and not raw_value.strip()
                        ):
                            if name in self._required_daily_fields:
                                raise ValueError(f"required daily field is empty: {name}")
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
                "calendar": calendar_sessions,
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
    """Collect official delisting records without owning persistence."""

    def __init__(self, config, provider=None):
        self.config = config
        self.provider = provider

    def collect(self, request):
        import re
        from protocal import normalize_security_id
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
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or self._http

    @staticmethod
    def _http(url):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def collect(self, request):
        from bs4 import BeautifulSoup
        start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
        if start > end:
            raise ValueError("start must not exceed end")
        records, raw, day = [], [], start
        try:
            while day <= end:
                url = f"https://cn.govopendata.com/xinwenlianbo/{day:%Y%m%d}/"
                page = self.transport(url)
                raw.append({"url": url, "request": {"date": day.isoformat()}, "response": page})
                soup = BeautifulSoup(page, "html.parser")
                articles = soup.select("article.content-section")
                if not articles:
                    raise ValueError("broadcast page contains no articles")
                for seq, article in enumerate(articles, 1):
                    heading = article.find(["h1", "h2", "h3"])
                    body = article.select_one(".content-body")
                    if heading is None or body is None:
                        raise ValueError("incomplete broadcast article")
                    headline = heading.get_text(strip=True)
                    content = "\n".join(p.get_text(strip=True) for p in body.find_all("p"))
                    if not headline or not content:
                        raise ValueError("empty broadcast article")
                    if request.get("keyword", "") not in headline + content:
                        continue
                    records.append({"date": day.isoformat(), "seq": seq,
                                    "section_id": (heading.get("id") or article.get("id") or "").removeprefix("section-"),
                                    "headline": headline, "content": content,
                                    "raw_page": len(raw) - 1})
                day += timedelta(days=1)
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {"records": records, "raw": raw, "data_as_of": end.isoformat(),
                "scope": {"start": start.isoformat(), "end": end.isoformat(),
                          "keyword": request.get("keyword", "")}}


def collect_daily(request):
    return BaostockDaily({}).collect(request)


DEFAULT_METHODS = {
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
    value = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return data.write("sources", "stock_basic", f"{_sha256(payload)}.json", value)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _optional(data, identity, name):
    from protocal import DataError
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
