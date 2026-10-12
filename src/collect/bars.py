"""Capture independently auditable public daily bars; never modify the lake.

Raw and HFQ are distinct requests. HFQ is a provider price index, not an
investor's share/cash ledger. Missing bars do NOT imply suspension. Current
security codes do not establish historical listing/renaming coverage.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from .data import saved


URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def make_request(code, exchange, start, end, basis):
    if exchange not in {"SH", "SZ", "BJ"} or len(code) != 6 or not code.isdigit():
        raise ValueError("invalid security identity")
    datetime.strptime(start, "%Y%m%d"); datetime.strptime(end, "%Y%m%d")
    if start > end or basis not in {"raw", "hfq"}:
        raise ValueError("invalid dates or price basis")
    return {"provider": "eastmoney", "url": URL, "code": code,
            "exchange": exchange, "price_basis": basis, "params": {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "0" if basis == "raw" else "2",
                "secid": f'{1 if exchange == "SH" else 0}.{code}',
                "beg": start, "end": end}}


def snapshot(request, raw):
    return {"schema": 1, "request": request, "request_sha256": digest(request),
            "raw_response": raw,
            "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest()}


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
    bars = []
    previous = None
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
        if not all(math.isfinite(v) for v in (op, close, high, low, hands, amount)):
            raise ValueError("nonfinite bar")
        if min(op, close, low) <= 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("invalid OHLC")
        if hands < 0 or amount < 0 or ((hands == 0) != (amount == 0)):
            raise ValueError("inconsistent turnover")
        bars.append({"code": request["code"], "date": day, "open": op,
                     "close": close, "high": high, "low": low,
                     "volume": hands * 100, "amount": amount,
                     "price_basis": request["price_basis"]})
    return bars


def capture(data, securities, start, end, basis, *, master_sha256, identity="bar_evidence", fetcher=None):
    from .quotes import fetch_bars
    transport = fetch_bars if fetcher is None else fetcher
    securities = sorted(securities)
    if not securities or len(securities) != len(set(securities)):
        raise ValueError("empty or duplicate security universe")
    manifest = {"schema": 1, "release_status": "NOT_RELEASED",
                "master_sha256": master_sha256,
                "basis": basis, "scope": "FULL_INPUT_MASTER", "responses": []}
    failed = 0
    for code, exchange in securities:
        request = make_request(code, exchange, start, end, basis)
        name = digest(request) + ".json"
        captured = saved(data, "sources", identity, name)
        if captured is not None:
            evidence = json.loads(captured)
            validate_snapshot(evidence, request)
        else:
            try:
                raw = transport(request)
            except OSError as exc:
                failed += 1
                manifest["responses"].append({"code": code, "status": "TRANSPORT_ERROR", "error": str(exc)})
                print(f"{code}: transport error", flush=True)
                continue
            evidence = snapshot(request, raw)
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
        except (ValueError, KeyError, TypeError) as exc:
            failed += 1
            record.update(status="REJECTED", error=str(exc))
        manifest["responses"].append(record)
        print(f'{code}: {record["status"]}, {record.get("rows", 0)} bars', flush=True)
    manifest["failed_or_empty"] = failed
    data.write("sources", identity, "manifest.json", canonical(manifest) + b"\n")
    print(f"captured {len(securities)} securities; {failed} failed or empty; NOT_RELEASED")
    return manifest
