"""Capture an unfiltered public announcement index, never strategy/ledger data.

Calendar-month windows are the primary query plan, not an error fallback.
Source failures stop the dependent window, not other independent windows.
Any failed window rejects the complete batch; no retry, refresh or fallback.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from .data import saved

URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
HEADERS = {"Referer": "https://www.cninfo.com.cn/"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def month_windows(period):
    start, end = map(date.fromisoformat, period.split("~"))
    if start > end:
        raise ValueError("inverted announcement period")
    while start <= end:
        last = min(end, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]))
        yield f"{start.isoformat()}~{last.isoformat()}"
        start = last + timedelta(days=1)


def fetch_page(request):
    import requests
    response = requests.post(URL, data=request, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.content


def load_capture(request, data, identity, fetcher):
    request_identity = {"url": URL, "headers": HEADERS, "request": request}
    request_hash = sha(encoded(request_identity))
    name = f"index_{request_hash}.json"
    captured = saved(data, "sources", identity, name)
    if captured is not None:
        payload = json.loads(captured)
    else:
        raw = fetcher(dict(request)).decode("utf-8")
        payload = {"schema": 1, **request_identity, "request_sha256": request_hash,
                   "raw_response": raw, "raw_response_sha256": sha(raw.encode())}
        data.write("sources", identity, name, encoded(payload) + b"\n")
    if payload.get("schema") != 1 or any(payload.get(k) != v for k, v in request_identity.items()):
        raise ValueError("captured request mismatch")
    if payload.get("request_sha256") != request_hash:
        raise ValueError("request hash mismatch")
    raw = payload.get("raw_response")
    if not isinstance(raw, str) or sha(raw.encode()) != payload.get("raw_response_sha256"):
        raise ValueError("response hash mismatch")
    return json.loads(raw), name


def parse_page(raw, request):
    total, more = raw.get("totalRecordNum"), raw.get("hasMore")
    rows = raw.get("announcements")
    if rows is None and total == 0:
        rows = []
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


def collect(template, data, identity="announcement_index", *, fetcher=fetch_page):
    if any(template.get(field, "") for field in ("stock", "secid", "column", "plate")):
        raise ValueError("unfiltered security/market query required")
    if int(template["pageSize"]) <= 0:
        raise ValueError("positive pageSize required")
    windows = list(month_windows(template["seDate"]))
    report = {"schema": 2, "query": template, "url": URL, "headers": HEADERS,
              "scope": "all matched records for configured title query; not all corporate actions",
              "planned_windows": windows, "attempted_windows": 0,
              "windows": 0, "pages": 0, "records": 0, "failures": [],
              "sources": [], "complete": False, "release_status": "NOT_RELEASED",
              "collector_sha256": sha(Path(__file__).read_bytes())}
    seen = set(); current = None
    try:
        for window in windows:
            number, expected_total = 1, None
            report["attempted_windows"] += 1
            try:
                while True:
                    current = dict(template, seDate=window, pageNum=str(number))
                    raw, name = load_capture(current, data, identity, fetcher)
                    report["sources"].append({"file": name, "sha256": sha(data.read("sources", identity, name)), "accepted": False})
                    total, more, keys = parse_page(raw, current)
                    if expected_total is not None and total != expected_total:
                        raise ValueError("announcement total changed within window")
                    if any(key in seen for key in keys) or len(set(keys)) != len(keys):
                        raise ValueError("duplicate announcement identity across capture")
                    expected_total = total; seen.update(keys)
                    report["sources"][-1]["accepted"] = True
                    report["pages"] += 1; report["records"] += len(keys)
                    print(f"{window} page {number}: {len(keys)} / {total}", flush=True)
                    if not more:
                        break
                    number += 1
                report["windows"] += 1
            except Exception as exc:
                report['failures'].append({'window':window,'request':current,
                                           'error_type':type(exc).__name__,'error':str(exc)})
                print(f"{window} FAILED: {type(exc).__name__}: {exc}",flush=True)
        if report['failures']:
            raise ValueError('; '.join(failure['error'] for failure in report['failures']))
        report["complete"] = True
    finally:
        report["run_id"] = sha(encoded(report))
        data.write("sources", identity, "manifest.json", encoded(report) + b"\n")
    return report
