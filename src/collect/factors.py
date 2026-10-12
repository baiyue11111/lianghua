#!/usr/bin/env python3
"""Bounded baostock collection for research corporate-action evidence.

This tool deliberately writes only successful responses.  A successful empty
response is recorded as ``status=empty``; transport/provider/iteration errors
leave no cache file, so a later run can resume the task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TIMEOUT = 30


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
        valid_prefix = (market == "sh" and number.startswith("6")) or (
            market == "sz" and number.startswith(("000", "001", "002", "003", "300", "301", "302")))
        if not valid_prefix or len(number) != 6 or not number.isdigit():
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
    fields = [str(f) for f in getattr(response, "fields", [])]
    rows = []
    with _timeout(timeout):
        while response.next():
            row = response.get_row_data()
            rows.append(list(row))
    if not _success(response):
        raise ProviderError(
            f"response iteration failed: {response.error_code} "
            f"{response.error_msg}", str(response.error_code))
    for row in rows:
        if len(row) != len(fields):
            raise ProviderError(
                f"response row length {len(row)} != field length {len(fields)}")
    return fields, rows


def fetch_one(request: Request, bs, timeout: int = DEFAULT_TIMEOUT) -> dict:
    code = _bs_code(request.code)
    with _timeout(timeout):
        if request.kind == "adjust":
            response = bs.query_adjust_factor(
                code=code, start_date=request.start, end_date=request.end)
        elif request.kind == "dividend":
            response = bs.query_dividend_data(
                code=code, year=request.year, yearType="operate")
        else:
            raise ValueError(f"unsupported provider kind: {request.kind}")

    if not _success(response):
        raise ProviderError(
            f"{request.kind} {code} failed: "
            f"{getattr(response, 'error_code', '')} "
            f"{getattr(response, 'error_msg', '')}", str(getattr(response, "error_code", "")))
    fields, rows = _response_rows(response, timeout)
    fields_hash = hashlib.sha256(",".join(fields).encode("utf-8")).hexdigest()
    response_code = str(getattr(response, "code", "")).lower()
    if response_code != code:
        raise ProviderError(
            f"provider code mismatch: requested {code}, received {response_code}")
    if "code" not in fields:
        raise ProviderError("provider response has no code field")
    code_index = fields.index("code")
    if any(row[code_index] != code for row in rows):
        raise ProviderError("provider row code does not match request")
    request_payload = {
        "kind": request.kind, "code": code, "start": request.start,
        "end": request.end, "year": request.year,
    }
    raw_response = {
        "error_code": str(getattr(response, "error_code", "")),
        "error_msg": str(getattr(response, "error_msg", "")),
        "code": response_code,
        "fields": fields,
        "rows": rows,
    }
    return {
        "schema": 2,
        "status": "ok" if rows else "empty",
        "request": request_payload,
        "request_sha256": _sha256(request_payload),
        "request_fields_sha256": fields_hash,
        "raw_response": raw_response,
        "raw_response_sha256": _sha256(raw_response),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def fetch_and_cache(request: Request, bs, data, timeout: int = DEFAULT_TIMEOUT):
    payload = fetch_one(request, bs, timeout=timeout)
    value = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    return data.write('sources', 'research_cache', f'{request.key}.json', value)


def _validate_cache(request: Request, payload: dict) -> None:
    """Validate a schema-2 cache before treating it as completed."""
    if payload.get("schema") != 2:
        raise CacheIntegrityError("unsupported or missing cache schema")
    expected_request = {
        "kind": request.kind,
        "code": _bs_code(request.code),
        "start": request.start,
        "end": request.end,
        "year": request.year,
    }
    if payload.get("request") != expected_request:
        raise CacheIntegrityError("cached request does not match requested task")
    if payload.get("request_sha256") != _sha256(expected_request):
        raise CacheIntegrityError("cached request hash mismatch")
    raw = payload.get("raw_response")
    if not isinstance(raw, dict):
        raise CacheIntegrityError("cached raw response is missing")
    if payload.get("raw_response_sha256") != _sha256(raw):
        raise CacheIntegrityError("cached raw response hash mismatch")
    fields = raw.get("fields")
    rows = raw.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise CacheIntegrityError("cached fields/rows are malformed")
    if raw.get("code") != expected_request["code"]:
        raise CacheIntegrityError("cached provider code does not match request")
    if str(raw.get("error_code")) != "0":
        raise CacheIntegrityError("cached raw response has provider error")
    if "code" not in fields:
        raise CacheIntegrityError("cached response has no provider code field")
    code_index = fields.index("code")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
        raise CacheIntegrityError("cached row length does not match fields")
    if any(row[code_index] != expected_request["code"] for row in rows):
        raise CacheIntegrityError("cached row provider code does not match request")
    expected_fields_hash = hashlib.sha256(",".join(fields).encode("utf-8")).hexdigest()
    if payload.get("request_fields_sha256") != expected_fields_hash:
        raise CacheIntegrityError("cached field hash mismatch")
    if payload.get("status") not in {"ok", "empty"}:
        raise CacheIntegrityError("cached status is invalid")
    if payload["status"] == "empty" and rows:
        raise CacheIntegrityError("empty cache contains rows")
    if payload["status"] == "ok" and not rows:
        raise CacheIntegrityError("ok cache contains no rows")


def pending_requests(requests: list[Request], data) -> list[Request]:
    from protocal import DataError
    pending = []
    for request in requests:
        try:
            payload = json.loads(data.read('sources', 'research_cache', f'{request.key}.json'))
        except DataError as error:
            if error.code == 'missing':
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
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    unique_codes = []
    seen_markets = set()
    for code in codes:
        normalized = _bs_code(code)
        if normalized not in seen_markets:
            seen_markets.add(normalized)
            unique_codes.append(code)
    codes = unique_codes
    if not codes:
        raise ValueError("--codes is required")
    if args.kind == "adjust":
        if not args.start or not args.end:
            raise ValueError("adjust requires --start and --end")
        return [Request("adjust", code, args.start, args.end, None)
                for code in codes]
    if not args.year:
        raise ValueError("dividend requires --year")
    return [Request("dividend", code, None, None, year)
            for code in codes for year in _years(args.year)]


def login_with_timeout(bs, timeout: int):
    with _timeout(timeout):
        login = bs.login()
    if str(getattr(login, "error_code", "")) != "0":
        raise ProviderError(f"login failed: {login.error_code} {login.error_msg}", str(login.error_code))
    return login


def main(argv=None, *, data=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("adjust", "dividend"))
    parser.add_argument("--codes", help="comma-separated 6-digit codes")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--year", help="YYYY or YYYY:YYYY for dividend data")
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--force", action="store_true", help="refresh existing cache files")
    args = parser.parse_args(argv)
    if data is None:
        from protocal import build_data
        data = build_data(Path(__file__).resolve().parents[2])
    try:
        requests = build_requests(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.force:
        requests = limit_pending(requests, data, args.max_tasks)
    elif args.max_tasks:
        requests = requests[:args.max_tasks]
    if not requests:
        print("no pending research requests")
        return 0
    import baostock as bs
    failed = 0
    try:
        login_with_timeout(bs, args.timeout)
        for request in requests:
            path = fetch_and_cache(request, bs, data, args.timeout)
            print(f"saved {path}", flush=True)
    except Exception as exc:
        failed = 1
        print(f"batch stopped; completed caches retained: {exc}", flush=True)
    finally:
        try:
            with _timeout(args.timeout):
                bs.logout()
        except Exception as exc:
            failed = 1
            print(f"logout failed: {exc}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
