"""Fetch methods with standard DataBus delivery and process logging."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from math import isfinite
import warnings
from protocol import (DataError, SQLQuery, TableDef, TableQuery, normalize_security_id, validate_facts,
                      _calendar_facts, _calendar_covers, _fact_date as fact_date)
import re
import calendar
import hashlib
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
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
    data.put_rows('collection_audit', [{'id': uuid4().hex, 'settings': json.dumps(payload, ensure_ascii=False)}],
                  keys=('id',), types={'id': 'VARCHAR', 'settings': 'JSON'})
    return data.save_fact(facts)


def _fact_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise CacheIntegrityError("provider date is not text")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise CacheIntegrityError(f"provider date is invalid: {value!r}") from error


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
    def payment(code, key, amount, html):
        from bs4 import BeautifulSoup
        from datetime import datetime
        soup = BeautifulSoup(html, 'html.parser')
        if soup.title is None or code.upper() not in soup.title.get_text():
            raise ValueError('payment security mismatch')
        headers = ['公布日期','财政年度','事项','股权登记日','除权除息日','红利发放日']
        tables = [table for table in soup.find_all('table')
                  if [cell.get_text(strip=True) for cell in table.find('tr').find_all(['td','th'])] == headers]
        if len(tables) != 1:
            raise ValueError('one cash payment table is required')
        matches = []
        for row in tables[0].find_all('tr')[1:]:
            cells = [cell.get_text(strip=True) for cell in row.find_all('td',recursive=False)]
            if len(cells) != 6:
                raise ValueError('cash payment fields are incomplete')
            if cells[3] == '--' or cells[4] == '--':
                continue
            dates = tuple(datetime.strptime(value,'%d/%m/%Y').date().isoformat() for value in cells[3:5])
            if dates != key:
                continue
            rate = re.fullmatch(r'10股派([0-9.]+)元\(含稅\)',cells[2])
            if rate is None or Decimal(rate[1]) != Decimal(str(amount)):
                raise ValueError('cash payment quantity mismatch')
            paid = datetime.strptime(cells[5],'%d/%m/%Y').date().isoformat()
            if paid < key[1]:
                raise ValueError('cash payment precedes operation')
            matches.append(paid)
        if len(matches) != 1:
            raise ValueError('one exact cash payment record is required')
        return matches[0]

    @staticmethod
    def notice(code, announced, text):
        """Read separately dated capital conversion and cash actions from a notice."""
        import unicodedata
        text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
        code = normalize_security_id(code)
        if set(re.findall(r'(?:证券|股票)代码[:：](\d{6})(?!\d)', text)) != {code.split('.')[1]}:
            raise ValueError('distribution notice security mismatch')
        def one(pattern):
            values = set(re.findall(pattern, text))
            if len(values) != 1:
                raise ValueError(f'distribution notice field is missing or ambiguous: {code} {announced} {pattern}')
            return values.pop()
        def day(label):
            value = one(label + r'(?:为)?[:：]?(\d{4}年\d{1,2}月\d{1,2}日)')
            return datetime.strptime(value, '%Y年%m月%d日').date().isoformat()
        if not re.search(r'每10股转增', text):
            cash = Decimal(one(r'每10股派([\d.]+)元(?:人民币)?现金[(]含税')) / 10
            registration, operation = day('股权登记日'), day('除息日')
            payment = datetime.strptime(one(r'(?:本次社会公众股股息于|本公司此次委托中国结算深圳分公司代派的(?:股息|A股股东现金红利)将于)(\d{4}年\d{1,2}月\d{1,2}日)通过股东托管证券(?:商|公司(?:[(]或其他托管机构[)])?)直接划入其资金[帐账]户'), '%Y年%m月%d日').date().isoformat()
            if not cash.is_finite() or cash <= 0 or not announced <= registration <= operation <= payment:
                raise ValueError('cash distribution terms conflict')
            rows = [dict(announce_date=announced,registration_date=registration,operation_date=operation,
                         payment_date=payment,stock_market_date=None,cash_per_share=float(cash),
                         stock_dividend_per_share=0.,reserve_to_stock_per_share=0.,cash_after_tax_text=None)]
            validate_facts(dict(schema=1,kind='dividend',code=code,start=None,end=None,year=operation[:4],
                                source_hash=_sha256(text),records=rows),kind='dividend',code=code,year=operation[:4])
            return rows
        reserve = Decimal(one(r'每10股转增([\d.]+)股')) / 10
        cash_pattern = r'资本公积转增股本后[^。；;]{0,150}?每10股(?:派送现金红利|派)([\d.]+)元[(]含税[)]'
        has_cash = bool(re.search(cash_pattern, text))
        if not has_cash and re.search(r'每10股(?:派送现金红利|派)[\d.]+元', text):
            raise ValueError('cash distribution basis is not explicit')
        registration = day('资本公积转增股本股权登记日' if has_cash else '股权登记日')
        operation = day('资本公积转增股本除权日' if has_cash else '除权日')
        listing = day('新增(?:无限售条件|可)?流通股份上市日')
        credit_pattern = r'(?:本次转增股份将于|股份到[帐账]日为)(\d{4}年\d{1,2}月\d{1,2}日)(?:直接记入股东证券帐户|[；;])'
        credited = datetime.strptime(one(credit_pattern), '%Y年%m月%d日').date().isoformat()
        if not reserve.is_finite() or reserve <= 0 or not announced <= registration <= operation <= min(listing,credited):
            raise ValueError('capital distribution terms conflict')
        rows = [dict(announce_date=announced, registration_date=registration, operation_date=operation,
                     payment_date=credited, stock_market_date=listing, cash_per_share=0.,
                     stock_dividend_per_share=0., reserve_to_stock_per_share=float(reserve), cash_after_tax_text=None)]
        if has_cash:
            cash = Decimal(one(cash_pattern)) / 10
            cash_registration, payment = day('派送现金红利股权登记日'), day('除息及现金红利发放日')
            if not cash.is_finite() or cash <= 0 or not announced <= cash_registration <= payment:
                raise ValueError('cash distribution terms conflict')
            rows.append(dict(announce_date=announced, registration_date=cash_registration, operation_date=payment,
                             payment_date=payment, stock_market_date=None, cash_per_share=float(cash),
                             stock_dividend_per_share=0., reserve_to_stock_per_share=0., cash_after_tax_text=None))
        validate_facts(dict(schema=1,kind='dividend',code=code,start=None,end=None,year=operation[:4],
                            source_hash=_sha256(text),records=rows), kind='dividend',code=code,year=operation[:4])
        return rows

    @staticmethod
    def parse(html):
        from io import StringIO
        import pandas as pd
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
        return table, _sha256(html)

    @staticmethod
    def normalize(base, html, *, end=None, load=None, resolve=None, pay=None, parsed=None):
        validate_facts(base, kind="dividend", code=base["code"], year=base["year"])
        table, digest = DividendHistory.parse(html) if parsed is None else parsed
        plans = table[table['进度'].astype(str) == '预案']
        unimplemented = set(plans['公告日期'].astype(str)) if len(plans) else set()
        implemented = table['进度'].astype(str) == '实施'
        year = table['除权除息日'].astype(str).str[:4]
        table = table[implemented & ((year == base['year']) if resolve is None else
                      ((table['公告日期'].astype(str).str[:4] <= base['year']) & (year >= base['year'])))]
        if end is not None:
            date.fromisoformat(end)
            table = table[table['除权除息日' if resolve is None else '公告日期'].astype(str) <= end]
        keys = set(zip(table['股权登记日'].astype(str), table['除权除息日'].astype(str)))
        result = deepcopy(base)
        records = [row for row in base['records'] if end is None or row['operation_date'] <= end]
        records = [row for row in records if row['announce_date'] not in unimplemented
                   or (row['registration_date'], row['operation_date']) in keys]
        result['records'] = [row for row in deepcopy(records)
                             if (row['registration_date'], row['operation_date']) in keys]
        quantities = (('cash_per_share', '派息(税前)(元)'), ('stock_dividend_per_share', '送股(股)'),
                      ('reserve_to_stock_per_share', '转增(股)'))
        merged, texts = {}, {}
        for row in result['records']:
            key = (row['registration_date'], row['operation_date'])
            if row['cash_after_tax_text']:
                texts.setdefault(key, set()).add(row['cash_after_tax_text'])
            for target, column in quantities:
                left, right = merged.get(key, {}).get(target), row[target]
                if left is not None and right is not None and left != right:
                    rates = table[(table['股权登记日'].astype(str) == key[0])
                                  & (table['除权除息日'].astype(str) == key[1])]
                    if len(rates) != 1:
                        raise ValueError('public market distribution rate is not unique')
                    value = _fact_number(rates.iloc[0][column])
                    amount = None if value is None else float(Decimal(str(value)) / Decimal(10))
                    if amount not in (left, right):
                        raise DataError('data_conflict', f'public market distribution rate mismatch: {key} {target}')
                    merged[key][target] = row[target] = amount
            merged[key] = Methods._merge_record(merged.get(key, {}), {**row, 'cash_after_tax_text': None}, key)
        result['records'] = [{**row, 'cash_after_tax_text': ' | '.join(sorted(texts.get(key, ()))) or None}
                             for key, row in merged.items()]
        details, resolved = {}, set()
        known = set(merged)
        for _, row in table.iterrows():
            key = (str(row['股权登记日']), str(row['除权除息日']))
            if key in known or load is None:
                continue
            from bs4 import BeautifulSoup
            notice = _fact_date(str(row['公告日期']))
            url = ('https://vip.stock.finance.sina.com.cn/corp/view/vISSUE_ShareBonusDetail.php?'
                   + urlencode({'stockid':base['code'].split('.')[1], 'type':1, 'end_date':notice}))
            details[url] = load(url)
            soup = BeautifulSoup(details[url], 'html.parser')
            if soup.title is None or base['code'].split('.')[1] not in soup.title.get_text():
                raise ValueError('dividend detail security mismatch')
            fields = {cells[0].get_text(strip=True):cells[1].get_text(strip=True)
                      for tr in soup.select('tr') if len(cells := tr.find_all('td', recursive=False)) == 2}
            detail_key = (fields.get('登记日'), fields.get('除息日'))
            if (detail_key == key and fields.get('红利/配股起始日（送、转股到账日）') in (None,'','--')
                    and pay is not None and Decimal(str(row['送股(股)'])) == 0
                    and Decimal(str(row['转增(股)'])) == 0 and Decimal(str(row['派息(税前)(元)'])) > 0):
                evidence = pay(key, Decimal(str(row['派息(税前)(元)'])))
                fields['红利/配股起始日（送、转股到账日）'] = _fact_date(evidence['payment_date'])
                details['payment:'+notice] = evidence
            if detail_key != key or fields.get('红利/配股起始日（送、转股到账日）') in (None,'','--'):
                if resolve is None:
                    raise ValueError(f'dividend detail date mismatch: {base["code"]} {notice} history={key} detail={detail_key}')
                evidence = resolve(notice, key[0])
                actions = evidence['records']
                action_keys = {(item['registration_date'],item['operation_date']) for item in actions}
                detail_rates = (('转增比例（10转增）','转增(股)'), ('送股比例（10送）','送股(股)'),
                                ('税前红利（报价币种）','派息(税前)(元)'))
                other_event = any(Decimal(str(row[column])).quantize(Decimal(str(value))) != Decimal(str(value))
                                  for label,column in detail_rates
                                  if fields.get(label) not in (None,'','--')
                                  if (value := _fact_number(fields[label])) is not None)
                missing_dates = all(value in (None,'','--') for value in detail_key)
                if key not in action_keys or (detail_key not in action_keys and not (other_event or missing_dates)) or len(action_keys) != len(actions):
                    raise ValueError('official actions do not identify the source event')
                for target, column in quantities:
                    amount = Decimal(str(row[column]))
                    total = sum((Decimal(str(item[target])) * 10 for item in actions), Decimal(0))
                    if not amount.is_finite() or total.quantize(amount) != amount:
                        raise ValueError('official action quantity conflicts with history')
                details['notice:'+notice] = evidence
                keys.remove(key)
                keys.update(action_keys)
                resolved.update(action_keys)
                result['records'].extend(actions)
                known.add(key)
                continue
            result['records'].append({'announce_date':notice, 'registration_date':key[0], 'operation_date':key[1],
                'payment_date':_fact_date(fields['红利/配股起始日（送、转股到账日）']),
                'stock_market_date':None if fields['上市日']=='--' else _fact_date(fields['上市日']),
                'cash_per_share':None, 'stock_dividend_per_share':None, 'reserve_to_stock_per_share':None,
                'cash_after_tax_text':None})
            known.add(key)
        if (records and not keys) or len(result['records']) != len(keys) or keys != {(row['registration_date'], row['operation_date']) for row in result['records']}:
            raise ValueError(f"implemented distribution dates are incomplete: {base['code']} {base['year']}")
        for record in result["records"]:
            if (record["registration_date"], record["operation_date"]) in resolved:
                continue
            matches = table[(table["除权除息日"].astype(str) == record["operation_date"])
                            & (table["股权登记日"].astype(str) == record["registration_date"])
                            & (table["进度"].astype(str) == "实施")]
            if len(matches) != 1:
                raise ValueError("distribution requires an exact record-date and ex-date match")
            row = matches.iloc[0]
            for target, source in quantities:
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
        result['records'] = sorted((row for row in result['records'] if row['operation_date'][:4] == base['year']
                                    and (end is None or row['operation_date'] <= end)),
                                   key=lambda row:(row['operation_date'],row['registration_date']))
        result["source_hash"] = _sha256({"base": base, "html_sha256": digest, "end": end, "details": details})
        validate_facts(result, kind="dividend", code=result["code"], year=result["year"])
        return result

    @staticmethod
    def collect(code, years, *, end, get=None):
        import requests
        get = requests.get if get is None else get
        from bs4 import BeautifulSoup
        code = normalize_security_id(code)
        url = 'https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/' + code.split('.')[1] + '.phtml'
        response = get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        response.encoding = 'gb18030'
        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        tables = soup.select('table#sharebonus_1')
        if (soup.title is None or code.split('.')[1] not in soup.title.get_text() or len(tables) != 1
                or not tables[0].select('tbody tr td')
                or any('下一页' in link.get_text() for link in soup.find_all('a'))):
            raise ValueError('complete security dividend history is required')
        details = {}
        def load(url):
            if url not in details:
                response = get(url, timeout=DEFAULT_TIMEOUT)
                response.raise_for_status()
                response.encoding = 'utf-8' if url.startswith('https://etnet.com.hk/') else 'gb18030'
                details[url] = response.text
            return details[url]
        def pay(key, amount):
            url = 'https://etnet.com.hk/www/sc/ashares/quote_dividend.php?' + urlencode({'code':code.split('.')[1]})
            html = load(url)
            return {'payment_date':DividendHistory.payment(code,key,amount,html),
                    'source':{'url':url,'html':html}}
        notices = {}
        def resolve(announced, registered):
            if announced not in notices:
                from io import BytesIO
                from pypdf import PdfReader
                from base64 import b64encode
                packet = CninfoAnnouncements({}).collect({'start':announced,'end':registered,
                                                         'codes':[code]})
                matches = [row for row in packet['records'] if row['code'] == code.split('.')[1]
                           and re.search(r'(分红|派息|转增|权益分派).*(?:实施|公告)', row['title'])]
                if len(matches) != 1:
                    raise ValueError(f'one official implementation notice is required: {code} {announced} matches={[(row["id"], row["title"]) for row in matches]}')
                url = matches[0]['url']
                response = get(url, timeout=DEFAULT_TIMEOUT)
                response.raise_for_status()
                if not response.content.startswith(b'%PDF-'):
                    raise ValueError('official distribution notice must be a PDF')
                text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
                published = date.fromisoformat(url.split('/')[-2]).isoformat()
                notices[announced] = {'records':DividendHistory.notice(code,published,text), 'source':{
                    'query':packet,'url':url,'sha256':hashlib.sha256(response.content).hexdigest(),
                    'pdf':b64encode(response.content).decode('ascii'),'text':text}}
            return notices[announced]
        facts = {}
        parsed = DividendHistory.parse(str(tables[0]))
        for year in years:
            base = {'schema':1,'kind':'dividend','code':code,'start':None,'end':None,
                    'year':str(year),'source_hash':_sha256(html),'records':[]}
            try:
                facts[str(year)] = DividendHistory.normalize(base, str(tables[0]), end=end, load=load, resolve=resolve, pay=pay, parsed=parsed)
            except Exception as error:
                raise CollectionError(f'year {year}: {error}') from error
        return {'url':url,'html':html,'details':details,'notices':notices}, facts


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
        from market import fact_ref
        try:
            with data.transaction(readonly=True):
                payload = data.fact(fact_ref(request.kind, request.code, request.start, request.end, request.year))
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
        name = digest(request)
        try:
            captured = _optional(data, identity, name)
        except Exception as error:
            if getattr(error, "code", None) != "missing":
                raise
            captured = None
        if captured is not None:
            evidence = {**captured, 'raw_response': _get_attachment(data, identity, name).decode('utf-8')}
            validate_snapshot(evidence, request)
        else:
            try:
                evidence = snapshot(request, transport(request))
            except OSError as error:
                failed += 1
                manifest["responses"].append({"code": code, "status": "TRANSPORT_ERROR", "error": str(error)})
                continue
            raw = evidence['raw_response']
            _put_attachment(data, identity, name, raw.encode(), 'application/json')
            _put_config(data, identity, name, {key: value for key, value in evidence.items()
                                               if key != 'raw_response'})
        record = {"code": code, "reference": 'attachment:' + _attachment_id(identity, name),
                  "sha256": evidence['raw_response_sha256']}
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
    _put_config(data, identity, 'manifest', manifest)
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


class SinaFactors:
    """Convert paired public factors within the security's actual active window."""

    @staticmethod
    def provider_code(code, changes):
        code, seen = normalize_security_id(code), set()
        while True:
            matches = [e for e in changes if e['exchange'].lower()+'.'+e['old_code'] == code]
            if not matches:
                return code
            if len(matches) != 1 or code in seen:
                raise ValueError('ambiguous or cyclic security rename')
            event = matches[0]
            if (event.get('holder_share_ratio') != 1 or event.get('holding_period_continues') is not True
                    or not str(event.get('source_url','')).startswith('https://static.cninfo.com.cn/finalpage/')):
                raise ValueError('registered rename identity and holding terms are required')
            seen.add(code)
            code = event['exchange'].lower()+'.'+event['new_code']

    @staticmethod
    def verify_renames(payload, facts):
        result = deepcopy(payload)
        for event in result['events']:
            exchange = event['exchange'].lower()+'.'
            old_code, new_code = exchange+event['old_code'], exchange+event['new_code']
            if old_code not in facts or new_code not in facts:
                continue
            if (event.get('holder_share_ratio') != 1 or event.get('holding_period_continues') is not True
                    or not str(event.get('source_url','')).startswith('https://static.cninfo.com.cn/finalpage/')):
                raise ValueError('registered rename identity and holding terms are required')
            old, new = facts[old_code], facts[new_code]
            for code, fact in ((old_code,old),(new_code,new)):
                validate_facts(fact,kind='adjust',code=code,start=fact['start'],end=fact['end'])
            cut = _fact_date(event['effective_date'])
            before = [r for r in old['records'] if r['date'] < cut]
            after = [r for r in new['records'] if r['date'] == cut]
            if not before or len(after) != 1 or not math.isclose(before[-1]['backward'],after[0]['backward'],rel_tol=1e-8,abs_tol=1e-8):
                raise ValueError('rename factor seam is missing or contradictory: '+old_code+' -> '+new_code)
            event['factor_basis_continuity_verified'] = True
            event['factor_basis_evidence'] = {'method':'equal_backward_factor_at_rename_seam',
                'back_factor':after[0]['backward'],'old_factor_file_sha256':_sha256(old),
                'new_factor_file_sha256':_sha256(new)}
        return result

    @staticmethod
    def collect(request, security, *, get=None, changes=()):
        import requests
        get = requests.get if get is None else get
        responses = {}
        provider_code = SinaFactors.provider_code(request.code, changes)
        for kind in ('qfq', 'hfq'):
            url = 'https://finance.sina.com.cn/realstock/company/' + provider_code.replace('.', '') + '/' + kind + '.js'
            response = get(url, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
            responses[kind] = response.text
        return {'responses':responses, 'security':security, 'changes':changes}, SinaFactors.normalize(request, security, responses, changes=changes)

    @staticmethod
    def normalize(request, security, payloads, *, changes=()):
        from bisect import bisect_right
        code = normalize_security_id(request.code)
        if request.kind != 'adjust' or set(payloads) != {'qfq', 'hfq'}:
            raise ValueError('paired adjustment responses are required')
        validate_facts(security, kind='security', code=code)
        stock = security['records'][0]
        if not stock['listing_date'] or stock['type'] != '1' or stock['status'] not in {'0', '1'}:
            raise ValueError('verified stock listing and status are required')
        if stock['status'] == '0' and not stock['delisting_date']:
            raise ValueError('inactive security requires its end date')
        listing = date.fromisoformat(stock['listing_date'])
        first = max(date.fromisoformat(request.start), listing)
        last = date.fromisoformat(request.end)
        if stock['delisting_date']:
            last = min(last, date.fromisoformat(stock['delisting_date']))
        if first > last:
            raise ValueError('adjustment range does not intersect the security window')
        series = {}
        provider_code = SinaFactors.provider_code(code, changes)
        for kind, text in payloads.items():
            prefix = re.match(r'\s*var\s+' + re.escape(provider_code.replace('.', '') + kind) + r'\s*=\s*', text)
            if prefix is None:
                raise ValueError('adjustment response security or kind mismatch')
            payload, _ = json.JSONDecoder().raw_decode(text[prefix.end():])
            rows = payload['data']
            if type(payload['total']) is not int or payload['total'] != len(rows) or not rows:
                raise ValueError('adjustment response count is incomplete')
            values = [(_fact_date(row['d']), _fact_number(row['f'], positive=True)) for row in rows]
            days = [day for day, _ in values]
            if any(day is None or value is None for day, value in values) or days != sorted(set(days), reverse=True):
                raise ValueError('adjustment response dates or values are invalid')
            series[kind] = list(reversed(values))
        days = [day for day, _ in series['qfq']]
        if days != [day for day, _ in series['hfq']] or days[0] > listing.isoformat():
            raise ValueError('paired adjustment history lacks matching dates or a listing anchor')
        products = [q[1] * h[1] for q, h in zip(series['qfq'], series['hfq'])]
        if not all(math.isfinite(value) and math.isclose(value, products[0], rel_tol=1e-10) for value in products):
            raise ValueError('forward and backward adjustment bases conflict')
        at = lambda day: bisect_right(days, day) - 1
        forward_base = series['qfq'][at(last.isoformat())][1]
        backward_base = series['hfq'][at(listing.isoformat())][1]
        anchors = {first.isoformat(), *(day for day in days if first.isoformat() < day <= last.isoformat())}
        for change in changes:
            if change['exchange'].lower()+'.'+change['new_code'] == code:
                cut = date.fromisoformat(change['effective_date'])
                if first <= cut <= last:
                    anchors.add(cut.isoformat())
        records = [{'date': day, 'forward': forward_base / series['qfq'][at(day)][1],
                    'backward': series['hfq'][at(day)][1] / backward_base}
                   for day in sorted(anchors)]
        fact = {'schema':1, 'kind':'adjust', 'code':code, 'start':request.start, 'end':request.end,
                'year':None, 'source_hash':_sha256({'responses':payloads, 'security':security, 'changes':changes}), 'records':records}
        _validate_cache(request, fact)
        return fact


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


class Methods:
    """Resolve a registered method, validate its packet, and publish it."""

    FIELDSETS = FIELDSETS

    def __init__(self, data, handlers: Mapping[str, object] | None = None):
        self.data = data
        self.handlers = dict(handlers or {})
        self._evidence = {}
        self.data.define(TableDef('collection_audit', {'id': 'VARCHAR', 'settings': 'JSON'}, ('id',)))
        import requests
        self._http = requests.Session()

    @staticmethod
    def _identity(source):
        identity = source.get('storage_id') or source.get('id')
        if not isinstance(identity, str) or not identity:
            raise DataError('invalid_identity', 'source identity is required')
        return identity

    def _source_table(self, identity, kind, rows=(), keys=(), types=None):
        """Resolve a named source binding, creating its typed table on first write."""
        try:
            reference = self.data.source(identity, kind)
        except DataError as error:
            if error.code != 'missing' or not rows:
                raise
            from market import table_name
            name = table_name(f'{identity}/{kind}')
            columns = set().union(*(row.keys() for row in rows))
            updates = tuple(sorted(columns - set(keys)))
            with self.data.transaction():
                self.data.put_rows(name, rows, keys=keys, updates=updates, types=types)
                reference = 'lake:' + name
                self.data.bind(identity, kind, reference)
        return reference

    @staticmethod
    def _types(rows, declared=None):
        types = dict(declared or {})
        columns = set().union(*(row.keys() for row in rows)) if rows else set()
        for name in columns - types.keys():
            values = [row[name] for row in rows if name in row and row[name] is not None]
            if not values:
                raise DataError('invalid_schema', f'explicit type required for all-NULL column {name}')
            kinds = {type(value) for value in values}
            if kinds <= {bool}:
                types[name] = 'BOOLEAN'
            elif kinds <= {int}:
                types[name] = 'BIGINT'
            elif kinds <= {int, float}:
                types[name] = 'DOUBLE'
            elif kinds <= {Decimal, int}:
                types[name] = 'DECIMAL(38,18)'
            elif kinds <= {str}:
                types[name] = 'VARCHAR'
            elif kinds <= {date}:
                types[name] = 'DATE'
            elif kinds <= {datetime}:
                types[name] = 'TIMESTAMP'
            else:
                raise DataError('invalid_schema', f'explicit relational type required for {name}')
        # Declared hints may name columns this batch does not carry (partial
        # packets); only the columns actually provided are written.
        return {name: kind for name, kind in types.items() if name in columns}

    def _source_rows(self, identity, kind='records'):
        with self.data.transaction(readonly=True):
            reference = self.data.source(identity, kind)
            return self.data.table(reference).to_pylist()

    def _replace_source_rows(self, identity, kind, rows, keys, types=None):
        reference = self._source_table(identity, kind, rows, keys, types)
        name = reference[5:]
        definition = self.data.definition(name)
        with self.data.transaction():
            if rows:
                self.data.put_rows(name, rows, keys=definition.keys, updates=definition.updates,
                                   types=types or {}, refresh=True)
        return reference

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
            current = self._source_rows(self._identity(source))
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
                stored = self.data.calendar(calendar_dataset)
            elif stored_name is not None:
                raise ValueError("daily source calendar requires a registered dataset")
            actual = _calendar_payload(packet_calendar) if packet_calendar is not None else None
            if stored is not None or actual is not None:
                sources = [item for item in (stored, actual) if item is not None]
                result["calendar"] = {
                    "sessions": sorted({day for item in sources for day in item["sessions"]}),
                    "coverage": _calendar_coverage([span for item in sources for span in item["coverage"]]),
                }
        validate_record = getattr(handler, 'validate_record', None)
        if validate_record is not None:
            try:
                for row in result['records']:
                    validate_record(row)
            except (DataError, TypeError, ValueError) as error:
                raise CollectionError(str(error), result['raw']) from error
        return result

    @staticmethod
    def _fact_ref(task):
        from market import fact_ref
        return fact_ref(task.kind, task.code, task.start, task.end, task.year)

    def _changes(self, manifest):
        reference = manifest.get('inputs', {}).get('code_changes')
        if reference is None:
            return []
        if not isinstance(reference, str):
            raise DataError('invalid_ref', 'code changes require a table reference')
        with self.data.transaction(readonly=True):
            if reference.startswith('lake:'):
                return self.data.table(reference).to_pylist()
            if reference.startswith('changes:'):
                return self.data.object(reference)['events']
        raise DataError('invalid_ref', 'code changes require a native table reference')

    def doctor(self, source, request):
        if source.get('data_type') != 'daily' and source.get('method') != 'daily':
            raise ValueError('doctor requires a registered daily source')
        request = self.prepare_request(source, request)
        dataset = source['dataset_id']
        manifest = self.data.manifest(dataset)
        calendar = self.data.calendar(dataset)
        request.setdefault('start', min(span['start'] for span in calendar['coverage']))
        request.setdefault('end', manifest['inputs']['adjustments']['end'])
        if not request.get('codes'):
            raise ValueError('doctor requires securities')
        registry = self.data.registry()
        markets = [m for m in registry.get('markets', ())
                   if m.get('dataset_id') == dataset and m.get('code_pattern')]
        if any(not any(re.fullmatch(m['code_pattern'], normalize_security_id(code)) for m in markets)
               for code in request['codes']):
            raise ValueError('doctor requires registered stock codes')
        changes_ref = manifest.get('inputs', {}).get('code_changes')
        changes = self._changes(manifest) if changes_ref else []
        rows, archived = [], set()
        source_ids = {m.get('collect', {}).get('securities', {}).get('source')
                      for m in markets} - {None}
        for source_id in source_ids:
            records = self._source_rows(source_id)
            from market import digest as table_digest
            event = self._evidence.get(table_digest(self.data.table(self.data.source(source_id, 'records'))))
            if event is None:
                continue
            incoming = {item['record_hash'] for item in event.get('incoming', [])}
            values = [row for row in records if any(re.fullmatch(m['code_pattern'], row['code']) for m in markets)]
            rows.extend(values)
            archived.update(row['code'] for row in values if _sha256(row) not in incoming)
        if changes_ref:
            raw, found = Catalogue.code_changes({'events': changes}, rows,
                archived & {normalize_security_id(code) for code in request['codes']},
                request['start'], request['end'])
        else:
            raw, found = None, {'events': []}
        pending = self.plan_facts(dataset, manifest, request, changes=found['events'])
        groups = {}
        for name, task in pending:
            groups.setdefault((task.code, task.kind), []).append((name, task))
        if changes_ref and found != {'events': changes}:
            self.log({'dataset': dataset, 'event': 'code_changes_checked', 'raw': raw})
            types = {'exchange': 'VARCHAR', 'effective_date': 'VARCHAR', 'old_code': 'VARCHAR',
                     'new_code': 'VARCHAR', 'announcement_date': 'VARCHAR', 'source_url': 'VARCHAR',
                     'evidence_sha256': 'VARCHAR', 'holder_share_ratio': 'BIGINT',
                     'holding_period_continues': 'BOOLEAN',
                     'factor_basis_continuity_verified': 'BOOLEAN'}
            types = {key: kind for key, kind in types.items()
                     if key in set().union(*(row.keys() for row in found['events']))}
            self._replace_source_rows(dataset, 'code_changes', found['events'],
                ('exchange', 'effective_date', 'old_code', 'new_code'), types)
            manifest['inputs']['code_changes'] = self.data.source(dataset, 'code_changes')
            self.data.save_manifest(dataset, manifest)
        errors = []
        original_end = manifest['inputs']['adjustments']['end']
        for (code, kind), tasks in groups.items():
            try:
                current = self.data.manifest(dataset)
                updated = self.complete_facts(dataset, current,
                    {**request, 'codes': [code]}, pending=tasks)
                updated['inputs']['adjustments']['end'] = original_end
                self.data.save_manifest(dataset, updated)
            except CollectionError as error:
                errors.append(str(error))
        if errors:
            raise CollectionError('; '.join(errors))
        updated = self.data.manifest(dataset)
        updated['inputs']['adjustments']['end'] = max(original_end, request['end'])
        self.data.save_manifest(dataset, updated)
        return {'dataset': dataset, 'status': 'complete', 'facts_complete': True,
                'codes': request['codes'], 'start': request['start'], 'end': request['end']}

    def plan_facts(self, dataset, manifest, request, *, changes=None):
        if not manifest.get('inputs'):
            return []
        inputs = manifest['inputs']
        registry = self.data.registry()
        patterns = [m['code_pattern'] for m in registry.get('markets', ())
                    if m.get('dataset_id') == dataset and m.get('code_pattern')]
        if not patterns:
            raise DataError('market_not_registered', 'stock fact collection requires a registered code rule')
        selected = {normalize_security_id(code) for code in request.get('codes', ())
                    if any(re.fullmatch(pattern, normalize_security_id(code)) for pattern in patterns)}
        if not selected:
            return []
        start = inputs['adjustments']['start']
        end = max(inputs['adjustments']['end'], request['end'])
        codes = sorted(selected if end == inputs['adjustments']['end'] else selected | {
            normalize_security_id(code) for code in manifest['scope']['symbols']
            if any(re.fullmatch(pattern, normalize_security_id(code)) for pattern in patterns)})
        calendar = self.data.calendar(dataset)
        first = min([request['start'], *(span['start'] for span in calendar['coverage'])])
        changes = self._changes(manifest) if changes is None else changes
        pending = []
        for code in codes:
            closed = None
            tasks = ([Request('security', code, None, None, None)] if code in selected else [])
            tasks.append(Request('adjust', code, start, end, None))
            if code in selected:
                tasks.extend(Request('dividend', code, None, None, str(year))
                             for year in range(int(first[:4]), int(end[:4]) + 1))
            for task in tasks:
                try:
                    with self.data.transaction(readonly=True):
                        fact = self.data.fact(self._fact_ref(task))
                    _validate_cache(task, fact)
                except DataError as error:
                    if error.code != 'missing':
                        raise
                    pending.append((self._fact_ref(task), task))
                    continue
                except (CacheIntegrityError, ValueError, KeyError, TypeError):
                    pending.append((self._fact_ref(task), task))
                    continue
                if task.kind == 'security':
                    closed = fact['records'][0]['delisting_date']
                advanced = (end > inputs['adjustments']['end'] and task.kind == 'dividend'
                            and task.year >= inputs['adjustments']['end'][:4]
                            and (closed is None or closed > inputs['adjustments']['end']))
                if advanced:
                    receipt = _optional(self.data, 'coverage', task.key)
                    if receipt is not None:
                        advanced = not (_fact_date(receipt['end']) >= end
                                        and receipt['sha256'] == _sha256(fact))
                refresh = advanced or bool(code in selected and request.get('refresh') and
                    (task.kind != 'dividend' or task.year >= request['start'][:4]))
                seams = {item['effective_date'] for item in changes
                         if item.get('exchange', '').lower() + '.' + item.get('new_code', '') == code
                         and start <= item['effective_date'] <= end}
                incomplete = (task.kind == 'adjust' and
                    (not fact['records'] or not seams <= {row['date'] for row in fact['records']}))
                incomplete |= task.kind == 'dividend' and any(
                    row[field] is None for row in fact['records']
                    for field in ('cash_per_share', 'stock_dividend_per_share', 'reserve_to_stock_per_share'))
                if refresh or incomplete:
                    pending.append((self._fact_ref(task), task))
        return pending

    def complete_facts(self, dataset, manifest, request, *, check=False, pending=None):
        if not manifest.get('inputs'):
            return True if check else manifest
        inputs = manifest['inputs']
        end = max(inputs['adjustments']['end'], request['end'])
        pending = self.plan_facts(dataset, manifest, request) if pending is None else pending
        if check:
            return not pending
        registry = self.data.registry()
        changes = self._changes(manifest)
        outputs, securities, dividends = [], {}, {}
        for reference, task in pending:
            try:
                if task.kind == 'security':
                    source_ids = {m['collect']['securities']['source'] for m in registry['markets']
                                  if m.get('dataset_id') == dataset and m.get('code_pattern')
                                  and re.fullmatch(m['code_pattern'], task.code)}
                    rows = [row for source_id in source_ids for row in self._source_rows(source_id)
                            if row.get('code') == task.code]
                    if len(rows) != 1:
                        raise ValueError('one registered security catalogue record is required: ' + task.code)
                    raw = rows[0]
                    fact = securities[task.code] = Catalogue.security(task.code, raw)
                elif task.kind == 'adjust':
                    if task.code not in securities:
                        securities[task.code] = self.data.fact(self._fact_ref(
                            Request('security', task.code, None, None, None)))
                    security = securities[task.code]
                    _validate_cache(Request('security', task.code, None, None, None), security)
                    raw, fact = SinaFactors.collect(task, security, get=self._http.get, changes=changes)
                else:
                    if task.code not in dividends:
                        years = [item.year for _, item in pending
                                 if item.kind == 'dividend' and item.code == task.code]
                        dividends[task.code] = DividendHistory.collect(
                            task.code, years, end=end, get=self._http.get)
                    raw, facts = dividends[task.code]
                    fact = facts[task.year]
                _validate_cache(task, fact)
                if task.kind == 'adjust' and not fact['records']:
                    raise CacheIntegrityError(f'no adjustment anchor for {task.code}')
            except Exception as error:
                raise CollectionError(f'{task.code} {task.kind}: {error}') from error
            outputs.append((reference, raw, fact, task))
        self.save_facts(dataset, manifest, outputs, end=end)
        updated = deepcopy(manifest)
        updated['inputs']['adjustments']['end'] = end
        return updated

    def save_facts(self, dataset, manifest, outputs, *, end=None):
        updated = deepcopy(manifest)
        updated.pop('files', None)
        payloads = []
        for reference, raw, fact, task in outputs:
            payloads.append(fact)
            self.log({'dataset': dataset, 'fact': reference, 'raw': raw})
            if task.kind == 'dividend':
                _put_config(self.data, 'coverage', task.key,
                            {'end': end, 'sha256': _sha256(fact)})
        # One grouped write: a transaction and a parquet file per payload is what
        # made a full-market fact collection take hours.
        self.data.save_facts(payloads)
        self.data.save_manifest(dataset, updated)
        return updated

    def coverage(self, source: Mapping, request: Mapping):
        if (source.get("data_type") != "daily" and source.get('method') != 'daily') or request.get("force") is True:
            return None
        symbols = tuple(request.get('codes', ()))
        fields = tuple(request.get('fields') or source.get('fields') or
                       ('open', 'high', 'low', 'close', 'volume', 'amount'))
        dataset = source.get('dataset_id', source['id'])
        return dict(self.daily_coverage(dataset, symbols, date.fromisoformat(request['start']),
            date.fromisoformat(request['end']), fields, identity=source.get('storage_id', source['id'])))

    def no_trade(self, manifest):
        dataset = manifest.get('dataset_id')
        if not dataset:
            raise DataError('invalid_manifest', 'no-trade source requires a dataset identity')
        with self.data.transaction(readonly=True):
            reference = self.data.source(dataset, 'no_trade')
            return self.data.table(reference).to_pylist()

    def daily_coverage(self, dataset: str, symbols: tuple[str, ...],
                       start: date, end: date, fields: tuple[str, ...], *, identity=None) -> Mapping[str, object]:
        with self.data.transaction(readonly=True):
            return self._daily_coverage_readonly(dataset, symbols, start, end, fields, identity=identity)

    def _daily_coverage_readonly(self, dataset: str, symbols: tuple[str, ...],
                                 start: date, end: date, fields: tuple[str, ...], *, identity=None) -> Mapping[str, object]:
        if (not symbols or not fields or start > end or
                any(not isinstance(value, str) or not value for value in (*symbols, *fields))):
            raise DataError('invalid_query', 'daily coverage arguments are invalid')
        manifest = self.data.manifest(dataset)
        calendar = self.data.calendar(dataset)
        sessions = tuple(date.fromisoformat(str(value)) for value in calendar.get('sessions', ()))
        wanted_days = tuple(day for day in sessions if start <= day <= end)
        requested_ids = {normalize_security_id(value) for value in symbols}
        declared = manifest['scope']['symbols']
        aliases = sorted({str(value) for value in (*symbols, *declared)
                          if normalize_security_id(str(value)) in requested_ids} | requested_ids)
        reference = self.data.source(identity or dataset, 'bars')
        table_name = reference[5:]
        definition = self.data.definition(table_name)
        columns = ('code', 'date', *fields)
        if not set(columns) <= definition.columns.keys():
            raise DataError('invalid_query', 'daily coverage fields are not registered')
        projection = ', '.join(self._sql_name(name) for name in columns)
        statement = (f'SELECT {projection} FROM {self._sql_name(table_name)} '
                     'WHERE code IN (SELECT UNNEST(?)) '
                     'AND CAST(date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)')
        with self.data.transaction(readonly=True):
            rows = self.data.scan(SQLQuery(statement, (aliases, start.isoformat(), end.isoformat()))).table.to_pylist()
        found = {(normalize_security_id(str(row['code'])), str(row['date'])[:10])
                 for row in rows if all(row.get(field) is not None for field in fields)}
        for item in self.no_trade({**manifest, 'dataset_id': dataset}):
            if normalize_security_id(str(item.get('code'))) not in requested_ids:
                continue
            for day in wanted_days:
                if item.get('start') <= day.isoformat() <= item.get('end'):
                    found.add((normalize_security_id(str(item['code'])), day.isoformat()))
        missing = {symbol: [day.isoformat() for day in wanted_days
                            if (normalize_security_id(symbol), day.isoformat()) not in found]
                   for symbol in symbols}
        calendar_complete = bool(sessions) and sessions[0] <= start <= end < sessions[-1]
        return {'dataset': dataset, 'start': start.isoformat(), 'end': end.isoformat(),
                'calendar_complete': calendar_complete, 'fields': tuple(fields),
                'complete': bool(wanted_days) and calendar_complete and not any(missing.values()),
                'missing': missing, 'sessions': tuple(day.isoformat() for day in wanted_days)}

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
        daily = source.get('method') == 'daily' or source.get('data_type') == 'daily'
        rejected = {item['code'] for item in result['failures']}
        rows = [row for row in packet['records'] if not rejected
                or (normalize_security_id(row['code']) if daily else row.get('code')) not in rejected]
        try:
            ref = result['ref']
            version = None if ref is None else (self.records(source, 0, 0)['snapshot_id'] if daily else ref.sha256)
            event = {'source_id': source['id'], 'snapshot_id': version,
                      'raw': packet['raw'], 'request': packet.get('request', {}),
                      'scope': packet.get('scope', {}), 'failures': result['failures'],
                      'incoming': [{'record_hash': _sha256({key: value for key, value in row.items() if key != 'raw_page'}),
                                    'raw_page': row.get('raw_page')} for row in rows],
                      'collected_at': datetime.now(timezone.utc).isoformat()}
            self.log(event)
            if version is not None:
                self._evidence.clear()
                self._evidence[version] = event
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
            return self._publish_daily(self._identity(source), source.get("dataset_id"), packet, dry_run=dry_run)
        if source.get("data_type") == "facts":
            if (not isinstance(getattr(handler, "value_columns", None), Mapping)
                    or not getattr(handler, "text_columns", None)
                    or len(getattr(handler, "dates", ())) != 2
                    or not getattr(handler, "keys", None)
                    or not callable(getattr(handler, "validate_record", None))):
                raise DataError("unsupported_facts_source", "facts provider must declare a numeric schema")
            return self._publish_facts(source, packet, handler, dry_run=dry_run)
        declared = source.get('params', {}).get('key_fields', getattr(handler, 'keys', None))
        if (not isinstance(declared, (list, tuple)) or not declared
                or any(not isinstance(name, str) or not name for name in declared)
                or len(set(declared)) != len(declared)):
            raise DataError('identity_policy', 'source must declare business key_fields')
        keys = tuple(declared)
        incoming = packet["records"]
        if any(not isinstance(row, Mapping) or any(row.get(key) is None for key in keys)
                        for row in incoming):
            raise DataError("invalid_packet", "record identity fields are required")
        try:
            old = self._source_rows(self._identity(source))
        except DataError as error:
            if error.code != 'missing':
                raise
            old = []
        records, failures = self.merge_records(old, incoming, keys,
            refresh=packet.get('request', {}).get('refresh') is True)
        if dry_run:
            return {'records': len(records), 'incoming': len(incoming), 'failures': failures}
        if records:
            declared_types = {**getattr(handler, 'types', {}),
                              **source.get('params', {}).get('types', {})}
            types = self._types(records, declared_types)
            self._replace_source_rows(self._identity(source), 'records', records, keys, types)
        rejected = {item['code'] for item in failures}
        return {'ref': None, 'records': sum(row.get('code') not in rejected for row in incoming),
                'failures': failures}

    @classmethod
    def merge_records(cls, old, incoming, keys, *, refresh=False, immutable=False):
        """Reject a conflicting code while retaining all other observed records."""
        def clean(row):
            return {name: deepcopy(value) for name, value in row.items() if name != 'raw_page'}
        def key(row):
            return tuple(_canonical(row[name]) for name in keys)
        stored = {}
        for row in old:
            identity = key(row)
            if identity in stored:
                raise DataError('data_conflict', 'duplicate stored identity: ' + repr(identity))
            stored[identity] = clean(row)
        current, failures = dict(stored), {}
        for row in incoming:
            value, identity = clean(row), key(row)
            code = value.get('code')
            if code in failures:
                continue
            try:
                if identity not in current:
                    current[identity] = value
                elif immutable:
                    if current[identity] != value:
                        raise DataError('data_conflict', 'record version is immutable: ' + repr(identity))
                else:
                    current[identity] = cls._merge_record(current[identity], value, identity, refresh=refresh)
            except DataError as error:
                if not isinstance(code, str) or not code:
                    raise
                failures[code] = str(error)
        current = {identity: row for identity, row in current.items() if row.get('code') not in failures}
        current.update((identity, row) for identity, row in stored.items() if row.get('code') in failures)
        return ([current[identity] for identity in sorted(current)],
                [{'code': code, 'error': error} for code, error in sorted(failures.items())])

    def _publish_facts(self, source, packet, handler, *, dry_run):
        source_id = self._identity(source)
        texts, numeric = tuple(handler.text_columns), tuple(handler.value_columns)
        if (len(set(texts + numeric)) != len(texts + numeric)
                or not {'code', 'date', *handler.keys, *handler.dates} <= set(texts)):
            raise DataError('invalid_schema', 'facts identity and time columns must be declared')
        incoming = [{name: deepcopy(item) for name, item in row.items() if name != 'raw_page'}
                    for row in packet['records']]
        try:
            old = self._source_rows(source_id, 'records')
        except DataError as error:
            if error.code != 'missing':
                raise
            old = []
        for row in old:
            handler.validate_record(row)
        records, failures = self.merge_records(old, incoming, handler.keys, immutable=True)
        rejected = {item['code'] for item in failures}
        accepted = sum(row['code'] not in rejected for row in incoming)
        if dry_run:
            return {'records': len(records), 'incoming': len(incoming), 'failures': failures}
        types = {name: 'VARCHAR' for name in texts}
        types.update({name: 'DOUBLE' for name in numeric})
        if records:
            types = self._types(records, types)
            self._replace_source_rows(source_id, 'records', records, handler.keys, types)
        return {'ref': None, 'records': accepted, 'failures': failures}

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
        with self.data.transaction(readonly=True):
            reference = self.data.source(self._identity(source), 'records')
            content = self.data.table(reference)
            rows = content.to_pylist()
        dates = [str(row[name]) for row in rows for name in ("date", "data_at", "published_at")
                 if row.get(name) is not None]
        from market import digest as table_digest
        version = table_digest(content)
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
        for event in reversed(tuple(self._evidence.values())):
            for binding in event.get('incoming', []):
                index = binding.get('raw_page')
                if binding.get('record_hash') == digest and type(index) is int and 0 <= index < len(event.get('raw', [])):
                    return [event['raw'][index]]
        with self.data.transaction(readonly=True):
            for row in self.data.scan(TableQuery('collection_audit')).table.to_pylist():
                event = json.loads(row['settings']) if isinstance(row['settings'], str) else row['settings']
                for binding in event.get('incoming', []):
                    index = binding.get('raw_page')
                    if (binding.get('record_hash') == digest and type(index) is int
                            and 0 <= index < len(event.get('raw', []))):
                        return [event['raw'][index]]
        return []

    def evidence(self, version):
        if version in self._evidence:
            return self._evidence[version]
        with self.data.transaction(readonly=True):
            for row in self.data.scan(TableQuery('collection_audit')).table.to_pylist():
                value = json.loads(row['settings']) if isinstance(row['settings'], str) else row['settings']
                if value.get('snapshot_id') == version and isinstance(value.get('raw'), list):
                    self._evidence[version] = value
                    return value
        return {}

    @staticmethod
    def _sql_name(name):
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def daily_rows(packet):
        """Validate supplied cells only. Absence is not a market observation."""
        request = packet.get('request', {})
        codes = {normalize_security_id(code) for code in request.get('codes', ())}
        fields = {'open', 'high', 'low', 'close', 'volume', 'amount'}
        merged = {}
        for row in packet['records']:
            try:
                code = normalize_security_id(row['code'])
                day = date.fromisoformat(row['date']).isoformat()
                if codes and code not in codes:
                    raise ValueError('daily response security mismatch')
                if request.get('start') and not request['start'] <= day <= request['end']:
                    raise ValueError('daily response date outside requested range')
                value = {key: item for key, item in row.items() if key != 'raw_page' and item is not None}
                value['date'] = day
                key = (code, day)
                if key in merged:
                    value = Methods._merge_daily_row(merged[key], value, key)
                for name in fields & value.keys():
                    number = value[name]
                    if (type(number) not in (int, float) or not isfinite(number) or number < 0
                            or name in {'open', 'high', 'low', 'close'} and number == 0):
                        raise ValueError('daily field is invalid: ' + name)
                low, high = value.get('low'), value.get('high')
                if low is not None and high is not None and low > high:
                    raise ValueError('daily OHLC values conflict')
                for name in ('open', 'close'):
                    if name in value and (low is not None and value[name] < low or high is not None and value[name] > high):
                        raise ValueError('daily OHLC values conflict')
                if 'volume' in value and 'amount' in value and (value['volume'] == 0) != (value['amount'] == 0):
                    raise ValueError('daily turnover fields conflict')
                if set(value) <= {'code', 'date'}:
                    continue
                merged[key] = value
            except (KeyError, TypeError, ValueError) as error:
                raise DataError('invalid_packet', str(error)) from error
        return merged

    def _publish_daily(self, identity: str, dataset_id: str | None, packet: Mapping[str, object], *, dry_run: bool = False) -> dict:
        """Merge validated bars into the source table registered for this source."""
        self._validate_canonical_daily(packet)
        incoming = list(self.daily_rows(packet).values())
        if not incoming:
            return {'incoming': 0, 'records': 0, 'failures': []} if dry_run else {'ref': None, 'records': 0, 'failures': []}
        try:
            old = self._source_rows(identity, 'bars')
        except DataError as error:
            if error.code != 'missing':
                raise
            old = []
        records, failures = self.merge_records(
            old, incoming, ('code', 'date'), refresh=packet.get('request', {}).get('refresh') is True)
        if dry_run:
            return {'incoming': len(incoming), 'records': len(records), 'failures': failures}
        calendar = packet.get('calendar')
        with self.data.transaction():
            if records:
                types = self._types(records, {'code': 'VARCHAR', 'date': 'DATE',
                    'open': 'DOUBLE', 'high': 'DOUBLE', 'low': 'DOUBLE', 'close': 'DOUBLE',
                    'volume': 'DOUBLE', 'amount': 'DOUBLE', 'price_basis': 'VARCHAR'})
                self._replace_source_rows(identity, 'bars', records, ('code', 'date'), types)
            if dataset_id and isinstance(calendar, Mapping):
                incoming_calendar = _calendar_payload(calendar)
                try:
                    current_calendar = self.data.calendar(dataset_id)
                except DataError as error:
                    if error.code != 'missing':
                        raise
                    current_calendar = {'sessions': [], 'coverage': []}
                merged_calendar = {
                    'sessions': sorted(set(current_calendar['sessions']) | set(incoming_calendar['sessions'])),
                    'coverage': _calendar_coverage([*current_calendar['coverage'], *incoming_calendar['coverage']]),
                }
                self.data.save_calendar(dataset_id, merged_calendar)
        rejected = {item['code'] for item in failures}
        return {'ref': None, 'records': sum(row.get('code') not in rejected for row in incoming),
                'failures': failures}

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


    def daily_collection_view(self, source: Mapping, dataset_id: str) -> Mapping[str, object]:
        """Return only canonical metadata; rows are fetched by page."""
        return self.daily_collection_page(source, dataset_id, offset=0, limit=0)

    def _daily_view_context(self, source: Mapping, dataset_id: str):
        with self.data.transaction(readonly=True):
            return self._daily_view_context_readonly(source, dataset_id)

    def _daily_view_context_readonly(self, source: Mapping, dataset_id: str):
        source_id = source['id']
        manifest = self.data.manifest(dataset_id)
        calendar = self.data.calendar(dataset_id)
        sessions = tuple(date.fromisoformat(str(item)) for item in calendar.get('sessions', ()))
        if len(sessions) < 2:
            raise DataError('invalid_dataset', 'daily calendar requires a following session')
        scope = dict(manifest.get('scope', {}))
        dataset_symbols = tuple(scope.get('symbols', ()))
        if not dataset_symbols:
            raise DataError('invalid_dataset', 'daily scope symbols are required')
        symbols = dataset_symbols
        source_codes = source.get('defaults', {}).get('codes')
        if isinstance(source_codes, list) and source_codes:
            requested = tuple(str(item) for item in source_codes)
            if not {normalize_security_id(item) for item in requested} <= {
                    normalize_security_id(item) for item in dataset_symbols}:
                raise DataError('scope_mismatch', 'registered source symbols are outside the dataset')
            symbols = requested
            scope['symbols'] = list(requested)
        identity = source.get('storage_id', source_id)
        reference = self.data.source(identity, 'bars')
        generation = {'source_id': source_id, 'dataset': dataset_id, 'reference': reference,
                      'evidence': self.data.evidence(reference), 'scope': scope,
                      'calendar': [day.isoformat() for day in sessions]}
        snapshot_id = hashlib.sha256(_bytes(generation)).hexdigest()
        return manifest, sessions, scope, symbols, reference, snapshot_id, dataset_symbols

    def daily_collection_summary(self, source: Mapping, dataset_id: str) -> Mapping[str, object]:
        source_id = source['id']
        manifest, sessions, scope, symbols, reference, snapshot_id, dataset_symbols = self._daily_view_context(
            source, dataset_id)
        table = reference[5:]
        codes = sorted({str(item) for item in symbols} | {normalize_security_id(str(item)) for item in symbols}
                       | {str(item) for item in dataset_symbols
                          if normalize_security_id(str(item)) in {normalize_security_id(str(code)) for code in symbols}})
        sql = (f'SELECT count(*) AS row_count, count(DISTINCT code) AS symbol_count, '
               f'min(date) AS first_date, max(date) AS last_date '
               f'FROM {self._sql_name(table)} WHERE code IN (SELECT UNNEST(?))')
        with self.data.transaction(readonly=True):
            row = self.data.scan(SQLQuery(sql, (codes,))).table.to_pylist()[0]
        first, last = row['first_date'], row['last_date']
        return {'snapshot_id': snapshot_id, 'source_id': source_id, 'collected_at': None,
                'request': {'dataset': dataset_id, 'symbols': list(symbols)},
                'data_as_of': str(last) if last is not None else sessions[-2].isoformat(),
                'scope': scope, 'records': [], 'records_count': int(row['row_count'] or 0),
                'symbols_count': int(row['symbol_count'] or 0),
                'first_date': str(first) if first is not None else None,
                'last_date': str(last) if last is not None else None}

    def daily_collection_page(self, source: Mapping, dataset_id: str, *, offset: int = 0,
                              limit: int = 50, start=None, end=None, symbols=None) -> Mapping[str, object]:
        source_id = source['id']
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 0:
            raise DataError('invalid_snapshot', 'invalid snapshot page')
        manifest, sessions, scope, declared, reference, snapshot_id, dataset_symbols = self._daily_view_context(
            source, dataset_id)
        start = sessions[0] if start is None else date.fromisoformat(str(start))
        end = sessions[-2] if end is None else date.fromisoformat(str(end))
        if not sessions[0] <= start <= end <= sessions[-2]:
            raise DataError('invalid_snapshot', 'snapshot dates are outside the daily calendar')
        requested = declared if symbols is None else tuple(str(item) for item in symbols)
        requested_ids = {normalize_security_id(item) for item in requested}
        if not requested_ids <= {normalize_security_id(item) for item in declared}:
            raise DataError('scope_mismatch', 'snapshot symbols are outside the daily scope')
        codes = sorted({str(item) for item in requested} | requested_ids |
                       {str(item) for item in dataset_symbols
                        if normalize_security_id(str(item)) in requested_ids})
        table = reference[5:]
        where = ('code IN (SELECT UNNEST(?)) '
                 'AND CAST(date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)')
        values = (codes, start.isoformat(), end.isoformat())
        with self.data.transaction(readonly=True):
            total = self.data.scan(SQLQuery(
                f'SELECT count(*) AS total FROM {self._sql_name(table)} WHERE {where}', values
            )).table.to_pylist()[0]['total']
            rows = [] if limit == 0 else self.data.scan(SQLQuery(
                f'SELECT * FROM {self._sql_name(table)} WHERE {where} '
                f'ORDER BY date, code LIMIT {limit} OFFSET {offset}', values
            )).table.to_pylist()
        records = []
        for row in rows:
            records.append({key: (value.isoformat() if isinstance(value, date) else
                                  None if isinstance(value, float) and not math.isfinite(value) else value)
                            for key, value in row.items()})
        return {'snapshot_id': snapshot_id, 'source_id': source_id, 'collected_at': None,
                'request': {'dataset': dataset_id, 'symbols': list(requested),
                            'start': start.isoformat(), 'end': end.isoformat()},
                'data_as_of': end.isoformat(), 'scope': scope, 'records': records,
                'records_count': int(total), 'offset': offset, 'limit': limit, 'total': int(total)}

    @staticmethod
    def validate(packet: Mapping) -> None:
        if not isinstance(packet, Mapping):
            raise ValueError("data packet must be an object")
        if not isinstance(packet.get("records"), list) or not isinstance(packet.get("raw"), list):
            raise ValueError("data packet must contain records and raw lists")
        if not isinstance(packet.get("scope"), Mapping):
            raise ValueError("data packet scope is required")
        if packet.get('data_type') == 'daily':
            Methods._validate_canonical_daily(packet)
            Methods.daily_rows(packet)

    def log(self, event: Mapping):
        if not isinstance(event, Mapping):
            raise TypeError("log event must be an object")
        from protocol import TableDef
        self.data.define(TableDef('collection_audit', {'id': 'VARCHAR', 'settings': 'JSON'}, ('id',)))
        value = json.dumps(dict(event), ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
        self.data.put_rows('collection_audit', [{'id': uuid4().hex, 'settings': value}],
                           keys=('id',))
        return dict(event)


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


    def last_dates(self, source: Mapping, codes) -> dict:
        """Return observed watermarks, independently for each requested security."""
        if source.get('method') == 'daily' or source.get('data_type') == 'daily':
            reference = self.data.source(source.get('storage_id', source['id']), 'bars')
            aliases = sorted({value for code in codes for value in
                              (code, normalize_security_id(code), normalize_security_id(code).split('.')[-1])})
            sql = (f'SELECT code, max(date) AS last_date FROM {self._sql_name(reference[5:])} '
                   'WHERE code IN (SELECT UNNEST(?)) GROUP BY code')
            with self.data.transaction(readonly=True):
                rows = self.data.scan(SQLQuery(sql, (aliases,))).table.to_pylist()
            dates = {}
            for row in rows:
                key = normalize_security_id(str(row['code']))
                day = str(row['last_date'])
                dates[key] = max(dates.get(key, day), day)
            return {code: dates[normalize_security_id(code)] for code in codes if normalize_security_id(code) in dates}
        dates = {}
        for row in self.record_rows(source):
            code, day = row.get('code'), row.get('date')
            if code in codes and day:
                dates[code] = max(dates.get(code, day), day)
        return dates

    def last_date(self, source: Mapping, codes) -> str | None:
        dates = self.last_dates(source, codes)
        return min(dates.values()) if dates and all(code in dates for code in codes) else None


    def calendar_coverage(self, source, start, end):
        """Return whether registered calendar facts cover the requested range."""
        dataset = source['dataset_id']
        with self.data.transaction(readonly=True):
            calendar = self.data.calendar(dataset)
        _, coverage = _calendar_facts(calendar)
        start_date = fact_date(start, 'calendar query start')
        end_date = fact_date(end, 'calendar query end')
        if start_date > end_date:
            raise ValueError('calendar query range is reversed')
        return _calendar_covers(coverage, start_date, end_date)


    def record_rows(self, source):
        with self.data.transaction(readonly=True):
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


class EastmoneyFundamentals:
    """A fixed numeric snapshot of Eastmoney financial report versions."""

    endpoint = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    text_columns = ("code", "date", "precision", "report_date", "notice_date", "update_date", "available_at")
    dates = ("report_date", "available_at")
    value_columns = {
        "roe": ("ROEJQ", "ratio", True),
        "debt_ratio": ("ZCFZL", "ratio", True),
        "revenue": ("TOTALOPERATEREVE", "cny", False),
        "net_profit": ("PARENTNETPROFIT", "cny", False),
        "eps": ("EPSJB", "cny_per_share", False),
        "book_value": ("BPS", "cny_per_share", False),
        "gross_margin": ("XSMLL", "ratio", True),
        "net_margin": ("XSJLL", "ratio", True),
        "revenue_growth": ("TOTALOPERATEREVETZ", "ratio", True),
        "profit_growth": ("PARENTNETPROFITTZ", "ratio", True),
        "cash_per_share": ("MGJYXJJE", "cny_per_share", False),
    }
    identity_columns = ("SECUCODE", "SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE")
    columns = (*identity_columns, "CURRENCY", *(item[0] for item in value_columns.values()))
    keys = ("code", "report_date", "available_at")
    page_size = 500

    def __init__(self, config, transport=None):
        self.config = config
        if transport is None:
            import requests
            transport = requests.Session().get
        self.transport = transport

    @staticmethod
    def _day(value, name):
        if not isinstance(value, str) or not value:
            raise ValueError(f"financial {name} is required")
        try:
            if "T" in value or " " in value:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.timetz().replace(tzinfo=None) != datetime.min.time():
                    raise ValueError("non-midnight time has no date precision")
                parsed = stamp.date()
            else:
                parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"financial {name} is invalid") from error
        return parsed.isoformat()

    @staticmethod
    def _code(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(?:SZ|SH)", value):
            raise ValueError("financial security identity is invalid")
        return normalize_security_id(value.split(".")[1].lower() + "." + value.split(".")[0])

    @staticmethod
    def _number(value, name):
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"financial {name} is not a finite number")
        return value

    @classmethod
    def validate_record(cls, row):
        if not isinstance(row, Mapping):
            raise ValueError("financial record must be an object")
        required = {"code", "date", "report_date", "notice_date", "update_date", "available_at",
                    "precision",
                    *cls.value_columns}
        if set(row) - (required | {"raw_page"}) or required - set(row):
            raise ValueError("financial record fields are incomplete")
        if (not isinstance(row["code"], str)
                or not re.fullmatch(r"(?:sh|sz)\.\d{6}", row["code"])
                or normalize_security_id(row["code"]) != row["code"]):
            raise ValueError("financial record code is not canonical")
        if row["precision"] != "date":
            raise ValueError("financial date precision must be date")
        report, notice, update = (cls._day(row[name], name) for name in
                                  ("report_date", "notice_date", "update_date"))
        if not report <= notice <= update or row["date"] != update:
            raise ValueError("financial record dates are inconsistent")
        available = (date.fromisoformat(max(notice, update)) + timedelta(days=1)).isoformat()
        if row["available_at"] != available + "T00:00:00+08:00":
            raise ValueError("financial availability boundary is inconsistent")
        for name, (_, _, ratio) in cls.value_columns.items():
            value = row[name]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"financial {name} is not finite")
        return True

    def collect(self, request):
        raw = []
        try:
            start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
            if start.isoformat() != request["start"] or end.isoformat() != request["end"] or start > end:
                raise ValueError("financial update date range is invalid")
            codes = request["codes"]
            if (not isinstance(codes, list) or not codes or len(codes) != len(set(codes))
                    or any(not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\.\d{6}", code)
                           for code in codes)):
                raise ValueError("financial codes must be unique canonical market codes")
            requested = {normalize_security_id(code): code for code in codes}
            fields = request.get("fields")
            if fields is not None and (not isinstance(fields, list) or not fields
                    or len(fields) != len(set(fields)) or set(fields) - set(self.value_columns)):
                raise ValueError("unsupported financial fields")
            east_codes = [code.split(".")[1] + "." + code.split(".")[0].upper()
                          for code in codes]
            params = {
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": ",".join(self.columns),
                "filter": f'(SECUCODE in ({",".join(json.dumps(code) for code in east_codes)}))'
                          f"(UPDATE_DATE>='{start.isoformat()}')(UPDATE_DATE<='{end.isoformat()}')(CURRENCY=\"CNY\")",
                "pageNumber": 1, "pageSize": self.page_size,
                "sortColumns": "UPDATE_DATE,REPORT_DATE,SECUCODE", "sortTypes": "1,1,1",
                "source": "HSF10", "client": "PC",
            }
            rows, expected_pages, expected_count = [], None, None
            page = 1
            while True:
                params["pageNumber"] = page
                response = self.transport(self.endpoint, params=params, timeout=DEFAULT_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                raw.append({"url": self.endpoint, "params": dict(params), "response": payload})
                if (page == 1 and isinstance(payload, dict) and payload.get("success") is False
                        and payload.get("code") == 9201 and payload.get("result") is None):
                    expected_pages, expected_count = 0, 0
                    break
                if (not isinstance(payload, dict) or payload.get("success") is not True
                        or payload.get("code") != 0 or not isinstance(payload.get("result"), dict)):
                    raise ValueError("Eastmoney financial response is malformed")
                result = payload["result"]
                pages, count, data = result.get("pages"), result.get("count"), result.get("data")
                if (type(pages) is not int or pages < 0 or type(count) is not int or count < 0
                        or not isinstance(data, list)):
                    raise ValueError("Eastmoney financial paging metadata is invalid")
                if expected_pages is None:
                    expected_pages, expected_count = pages, count
                    if pages != math.ceil(count / self.page_size):
                        raise ValueError("Eastmoney financial page count disagrees with total")
                elif pages != expected_pages or count != expected_count:
                    raise ValueError("Eastmoney financial paging metadata changed")
                if len(data) > self.page_size or (page < pages and len(data) != self.page_size):
                    raise ValueError("Eastmoney financial page is incomplete")
                rows.extend(data)
                if page == max(1, pages):
                    break
                page += 1
            if len(rows) != expected_count:
                raise ValueError("Eastmoney financial result count differs from total")
            records, seen = [], set()
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(self.columns) - set(row):
                    raise ValueError("Eastmoney financial row is missing projected columns")
                code = self._code(row["SECUCODE"])
                if code not in requested or row["SECURITY_CODE"] != code.split(".")[1]:
                    raise ValueError("Eastmoney financial row identity differs from request")
                report = self._day(row["REPORT_DATE"], "report date")
                notice = self._day(row["NOTICE_DATE"], "notice date")
                update = self._day(row["UPDATE_DATE"], "update date")
                if not report <= notice <= update or not start.isoformat() <= update <= end.isoformat():
                    raise ValueError("Eastmoney financial row dates are inconsistent")
                if row["CURRENCY"] != "CNY":
                    raise ValueError("Eastmoney financial currency is not CNY")
                available = (date.fromisoformat(max(notice, update)) + timedelta(days=1)).isoformat()
                record = {"code": code, "date": update, "precision": "date", "report_date": report,
                          "notice_date": notice, "update_date": update,
                          "available_at": available + "T00:00:00+08:00", "raw_page": index // self.page_size}
                for name, (column, _, ratio) in self.value_columns.items():
                    value = self._number(row[column], name)
                    record[name] = value / 100 if value is not None and ratio else value
                identity = (record["code"], record["report_date"], record["available_at"])
                if identity in seen:
                    raise ValueError("duplicate Eastmoney financial version")
                seen.add(identity)
                records.append(record)
            records.sort(key=lambda row: (row["update_date"], row["report_date"], row["code"]))
            return {"records": records, "raw": raw,
                    "data_as_of": max((row["available_at"] for row in records), default=None),
                    "scope": {"codes": list(codes), "update_date": {"start": start.isoformat(),
                               "end": end.isoformat()}, "fields": list(self.value_columns),
                               "availability": "conservative next-day boundary",
                               "observed_records": len(records),
                               "flow_basis": "reported cumulative amounts; no annualization or TTM"}}
        except Exception as error:
            raise CollectionError(str(error), raw) from error


class BaostockDaily:
    _last_request = None
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
        params = config.get('params', {})
        self.interval = params.get('interval', 1)
        self.retry_delay = params.get('retry_delay', 30)
        self.retries = params.get('retries', 2)
        for value in (self.interval, self.retry_delay):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('BaoStock request intervals must be positive seconds')
        if type(self.retries) is not int or not 0 <= self.retries <= 5:
            raise ValueError('BaoStock retries must be an integer from 0 to 5')

    def _call(self, query, *args, **kwargs):
        for attempt in range(self.retries + 1):
            previous = BaostockDaily._last_request
            if previous is not None:
                time.sleep(max(0, self.interval - (time.monotonic() - previous)))
            BaostockDaily._last_request = time.monotonic()
            try:
                result = query(*args, **kwargs)
            except OSError:
                if attempt == self.retries:
                    raise
            else:
                if str(result.error_code) not in {'10002001','10002002','10002003','10002004','10002005','10002006','10002007','10002008'} or attempt == self.retries:
                    return result
            time.sleep(self.retry_delay * 2 ** attempt)

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
    def trade_calendar(bs, start: str, end: str, *, call=None):
        try:
            start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError) as error:
            raise ValueError("calendar dates must be ISO dates") from error
        if start_day > end_day:
            raise ValueError("calendar start must not exceed end")
        response = (bs.query_trade_dates if call is None else call)(start_date=start, end_date=end)
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
        login = self._call(bs.login)
        if login.error_code != "0":
            raise RuntimeError(f"login {login.error_code}: {login.error_msg}")
        records, raw = [], []
        try:
            calendar = {"sessions": [], "coverage": []}
            if frequency == "d":
                calendar_end = request.get("calendar_end") or (date.fromisoformat(request['end']) + timedelta(days=31)).isoformat()
                all_sessions, calendar_raw = self.trade_calendar(
                    bs, request["start"], calendar_end,
                    call=lambda **kw: self._call(bs.query_trade_dates, **kw))
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
                response = self._call(bs.query_history_k_data_plus,
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
            stock = ''
            codes = {normalize_security_id(code).split('.')[1] for code in request.get('codes', ())}
            if codes:
                url = 'https://www.cninfo.com.cn/new/data/szse_stock.json'
                directory = self.transport(url)
                raw.append({'url':url,'response':directory})
                identities = []
                for code in sorted(codes):
                    rows = [row for row in directory['stockList'] if row['code'] == code]
                    if len(rows) != 1 or not isinstance(rows[0].get('orgId'),str) or not rows[0]['orgId']:
                        raise ValueError('one official announcement security identity is required: '+code)
                    identities.append(code+','+rows[0]['orgId'])
                stock = ';'.join(identities)
            while True:
                query = {"pageNum": str(page), "pageSize": "30",
                         "seDate": request["start"] + "~" + request["end"],
                         "tabName": "fulltext", "column": "szse", "plate": "",
                         "stock": stock, "searchkey": request.get("keyword", ""),
                         "category": self.config.get("category", ""), "isHLtitle": "true"}
                url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
                response = self.transport(url, urlencode(query).encode())
                raw.append({"url": url, "request": query, "response": response})
                rows, total, more = parse_cninfo_page(response, page, 30)
                if expected is not None and total != expected:
                    raise ValueError("announcement total changed during pagination")
                expected = total
                for row in rows:
                    if codes and row["code"] not in codes:
                        raise ValueError("announcement response security mismatch")
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
                              "pages": page, "total": expected}}
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


class FredSeries:
    """FRED daily observations; gaps remain gaps, never synthetic trade bars."""

    def __init__(self, config, transport=None):
        self.transport = self.get if transport is None else transport

    @staticmethod
    def get(url):
        import requests
        response = requests.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.text

    def collect(self, request):
        import csv
        import io
        codes = Methods._request_items(request['codes'], 'codes')
        if request.get('fields', ['close']) != ['close']:
            raise ValueError('FRED series only provides close')
        start, end = date.fromisoformat(request['start']), date.fromisoformat(request['end'])
        if start > end:
            raise ValueError('series date range is reversed')
        records, raw, sessions = [], [], set()
        for code in codes:
            code = normalize_security_id(code)
            market, series = code.split('.')
            if market != 'fred' or not re.fullmatch(r'[a-z][a-z0-9_]*', series):
                raise ValueError('FRED series identity is invalid')
            series = series.upper()
            url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?' + urlencode(
                {'id': series, 'cosd': start.isoformat(), 'coed': end.isoformat()})
            text = self.transport(url)
            rows = csv.DictReader(io.StringIO(text))
            if rows.fieldnames != ['observation_date', series]:
                raise ValueError('FRED response series identity differs')
            raw.append({'provider': 'FRED', 'request': {'url': url}, 'response': text})
            previous = None
            count = 0
            for row in rows:
                day = date.fromisoformat(row['observation_date'])
                if not start <= day <= end or previous is not None and day <= previous:
                    raise ValueError('FRED dates must be unique, ordered and inside request')
                previous = day
                if row[series] in ('', '.'):
                    continue
                value = float(row[series])
                if not isfinite(value) or value <= 0:
                    raise ValueError('FRED close must be finite and positive')
                records.append({'code': code, 'date': day.isoformat(), 'close': value})
                sessions.add(day.isoformat()); count += 1
            if not count:
                raise ValueError('FRED request has no observations: ' + series)
        return {'records': records, 'raw': raw, 'fields': ['close'], 'frequency': 'daily',
                'price_basis': 'raw', 'scope': {'codes': codes, 'start': str(start), 'end': str(end)},
                'calendar': {'sessions': sorted(sessions),
                             'coverage': [{'start': str(start), 'end': str(end)}]}}


class SinaDaily:
    """Sina's original unadjusted daily bars, in shares and CNY."""
    fields = ('open','high','low','close','volume','amount')

    def __init__(self, config, transport=None):
        self.transport = self.get if transport is None else transport

    @staticmethod
    def get(url):
        import requests
        response = requests.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.text

    @staticmethod
    def decode(code, text):
        from py_mini_racer import MiniRacer
        from akshare.stock.cons import hk_js_decode
        code = normalize_security_id(code)
        match = re.match(r'\s*var\s+KLC_K2_' + re.escape(code.replace('.','')) + r'\s*=\s*', text)
        if match is None:
            raise ValueError('daily response security mismatch')
        encoded, end = json.JSONDecoder().raw_decode(text[match.end():])
        if not isinstance(encoded,str) or not encoded or not re.fullmatch(r'\s*;\s*(?:/\*[\s\S]*\*/)?\s*',text[match.end()+end:]):
            raise ValueError('daily response format changed')
        with MiniRacer() as js:
            js.eval(hk_js_decode)
            return js.call('d', encoded)

    @staticmethod
    def normalize(code, rows, request):
        code = normalize_security_id(code)
        start, end = date.fromisoformat(request['start']), date.fromisoformat(request['end'])
        if start > end or not isinstance(rows, list):
            raise ValueError('daily range or response is invalid')
        fields = set(request.get('fields', SinaDaily.fields)) - {'date', 'code'}
        result, previous = [], None
        for row in rows:
            value = row['date']
            day = date.fromisoformat(value[:10])
            if value not in (day.isoformat(), day.isoformat()+'T00:00:00.000Z') or previous is not None and day <= previous:
                raise ValueError('daily dates must be unique and increasing')
            previous = day
            if start <= day <= end:
                result.append({'code': code, 'date': day.isoformat(),
                               **{field: row[field] for field in fields if row.get(field) is not None}})
        return list(Methods.daily_rows({'records': result, 'request': {**request, 'codes': [code]}}).values())

    def collect(self, request):
        codes = [normalize_security_id(code) for code in request['codes']]
        fields = set(request.get('fields', self.fields)) - {'date', 'code'}
        if (request.get('frequency', 'd') not in {'d', 'daily'} or request.get('adjustment', '3') != '3'
                or not fields or not fields <= set(self.fields) | {'tradestatus'}):
            raise ValueError('Sina daily supports only unadjusted daily fields')
        records, raw = [], []
        try:
            for code in codes:
                url = 'https://finance.sina.com.cn/realstock/company/' + code.replace('.', '') + '/hisdata_klc2/klc_kl.js'
                text = self.transport(url)
                raw.append({'provider': 'sina', 'url': url, 'response': text,
                            'price_basis': 'raw', 'volume_unit': 'share', 'amount_unit': 'CNY'})
                rows = self.normalize(code, self.decode(code, text), request)
                records.extend({**row, 'raw_page': len(raw)-1} for row in rows)
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {'records': records, 'raw': raw, 'price_basis': 'raw', 'frequency': 'daily',
                'scope': {'codes': codes, 'start': request['start'], 'end': request['end']},
                'data_as_of': max((row['date'] for row in records), default=None)}


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
    """Official security catalogues published through the shared method packet."""

    @staticmethod
    def code_changes(payload, rows, codes, start, end):
        from io import BytesIO
        from base64 import b64encode
        from pypdf import PdfReader
        import requests
        known = {event['exchange'].lower()+'.'+event['old_code'] for event in payload['events']}
        pending = set(codes) - known
        if not pending:
            return None, payload
        query = CninfoAnnouncements({}).collect({'start':start,'end':end,'keyword':'证券代码'})
        result, raw = deepcopy(payload), {'query':query,'notices':[]}
        by_code = {row['code']:row for row in rows}
        for row in query['records']:
            candidates = [code for code in pending if code.split('.')[1] == row['code']]
            if not candidates or '实施' not in row['title'] or '变更' not in row['title']:
                continue
            if len(candidates) != 1:
                raise ValueError('announcement security identity is ambiguous')
            code = candidates[0]
            response = requests.get(row['url'],timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
            if not response.content.startswith(b'%PDF-'):
                raise ValueError('code-change evidence must be an official PDF')
            text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
            events = Catalogue.rename(code.split('.')[0].upper(),row['url'].split('/')[-2],row['url'],text)
            matched = [event for event in events if event['old_code'] == code.split('.')[1]]
            if len(matched) != 1:
                raise ValueError('code-change notice does not identify the requested security')
            event = matched[0]
            new_code = code.split('.')[0]+'.'+event['new_code']
            if (new_code not in by_code or by_code[code]['ipoDate'] != by_code[new_code]['ipoDate']
                    or by_code[code]['outDate'] != event['effective_date']):
                raise ValueError('official code-change notice conflicts with security dates')
            evidence = {'url':row['url'],'pdf':b64encode(response.content).decode(),
                        'sha256':hashlib.sha256(response.content).hexdigest(),'text':text}
            event['evidence_sha256'] = _sha256(evidence)
            raw['notices'].append(evidence)
            result['events'].append(event)
            pending.remove(code)
        return raw, result

    @staticmethod
    def rename(exchange, announced, url, text):
        import unicodedata
        text = re.sub(r'(?m)^\s*\d+\s*/\s*\d+\s*$','',text)
        text = re.sub(r'\s+','',unicodedata.normalize('NFKC',text))
        pairs = set(re.findall(r'证券代码由[“"]([0-9/]+)[”"]变更为[“"]([0-9/]+)[”"]',text))
        days = set(re.findall(r'变更后的证券简称和证券代码启用日期[:：](\d{4}年\d{1,2}月\d{1,2}日)',text))
        if (exchange not in ('SH','SZ') or len(pairs) != 1 or len(days) != 1
                or '股份数量不变' not in text or '持股期限将持续计算' not in text
                or not url.startswith('https://static.cninfo.com.cn/finalpage/')):
            raise ValueError('explicit official rename and holding terms are required')
        effective = datetime.strptime(days.pop(),'%Y年%m月%d日').date().isoformat()
        if date.fromisoformat(announced).isoformat() > effective:
            raise ValueError('rename announcement follows its effective date')
        old, new = (part.split('/') for part in pairs.pop())
        if len(old) != len(new) or len(set(old+new)) != len(old+new) or any(not re.fullmatch(r'\d{6}',c) for c in old+new):
            raise ValueError('rename security identities are ambiguous')
        return [dict(exchange=exchange,old_code=a,new_code=b,effective_date=effective,
                     announcement_date=announced,source_url=url,holder_share_ratio=1,
                     holding_period_continues=True,factor_basis_continuity_verified=False)
                for a,b in zip(old,new)]

    @staticmethod
    def security(code, row):
        code = normalize_security_id(code)
        if row.get('code') != code or row.get('type') != '1' or row.get('status') not in {'0','1'}:
            raise ValueError('catalogue security identity or stock status is invalid')
        listing = date.fromisoformat(row['ipoDate']).isoformat()
        end = date.fromisoformat(row['outDate']).isoformat() if row.get('outDate') else None
        if (row['status'] == '0' and end is None) or (end is not None and end < listing):
            raise ValueError('catalogue security end date is invalid')
        fact = {'schema':1,'kind':'security','code':code,'start':None,'end':None,'year':None,
                'source_hash':_sha256({'catalogue':row}), 'records':[{'listing_date':listing,
                'delisting_date':end,'type':row['type'],'status':row['status']}]}
        validate_facts(fact, kind='security', code=code)
        return fact

    @staticmethod
    def normalize(exchange, listed, rows, page):
        """Convert official exchange rows without inventing listing dates."""
        fields = (('A_STOCK_CODE', 'COMPANY_ABBR', 'LIST_DATE', 'DELIST_DATE') if exchange == 'sh'
                  else ('A股代码', 'A股简称', 'A股上市日期', None) if listed
                  else ('证券代码', '证券简称', '上市日期', '终止上市日期'))
        records = []
        for row in rows:
            code, name, start, end = (row.get(key) if key else None for key in fields)
            if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or not isinstance(name, str) or not name.strip() or name == '-':
                raise ValueError('invalid official security identity')
            dates = []
            for value in (start,) if listed else (start, end):
                value = str(value)
                dates.append(datetime.strptime(value, '%Y%m%d' if exchange == 'sh' else '%Y-%m-%d').date().isoformat())
            if not listed and dates[1] < dates[0]:
                raise ValueError('delisting precedes listing')
            records.append(dict(code=exchange+'.'+code, code_name=name, ipoDate=dates[0],
                                outDate='' if listed else dates[1], type='1', status='1' if listed else '0', raw_page=page))
        if not records or len({row['code'] for row in records}) != len(records):
            raise ValueError('empty or duplicate official securities')
        return records

    keys = ('code',)

    def __init__(self, config):
        self.config = config

    def collect(self, request):
        import requests
        from io import BytesIO
        import pandas as pd
        exchanges = request.get('exchanges', ['sh','sz','bj'])
        if not isinstance(exchanges, (list, tuple)) or not exchanges or set(exchanges) - {'sh','sz','bj'}:
            raise ValueError('unsupported catalogue exchanges')
        raw, records = [], []
        try:
            for exchange in exchanges:
                if exchange == 'bj':
                    packet = BseCatalogue({}).collect({})
                    records.extend({**row,'raw_page':row['raw_page']+len(raw)} for row in packet['records'])
                    raw.extend(packet['raw'])
                    continue
                for listed in (True, False):
                    if exchange == 'sh':
                        url = 'https://query.sse.com.cn/sseQuery/commonQuery.do'
                        params = {'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L','STOCK_TYPE':'1,8',
                                  'COMPANY_STATUS':'2,4,5,7,8' if listed else '3','type':'inParams',
                                  'isPagination':'true','pageHelp.pageSize':10000,'pageHelp.pageNo':1}
                        response = requests.get(url, params=params, headers={
                            'Referer':'https://www.sse.com.cn/assortment/stock/list/share/'}, timeout=DEFAULT_TIMEOUT)
                        response.raise_for_status()
                        payload = response.json()
                        rows = payload['result']
                        raw.append({'provider':'sse','url':url,'params':params,'response':payload})
                        if len(rows) != int(payload['pageHelp']['total']):
                            raise ValueError('incomplete official security catalogue')
                    else:
                        url = 'https://www.szse.cn/api/report/ShowReport'
                        params = {'SHOWTYPE':'xlsx','CATALOGID':'1110' if listed else '1793_ssgs',
                                  'TABKEY':'tab1' if listed else 'tab2'}
                        response = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                        response.raise_for_status()
                        rows = pd.read_excel(BytesIO(response.content), dtype=str).fillna('').to_dict('records')
                        raw.append({'provider':'szse','url':url,'params':params,'response':rows,
                                    'response_sha256':hashlib.sha256(response.content).hexdigest()})
                    records.extend(self.normalize(exchange, listed, rows, len(raw)-1))
            if len({row['code'] for row in records}) != len(records):
                raise ValueError('overlapping official security catalogues')
            return {'records':records,'raw':raw,'scope':{'exchanges':list(exchanges)},
                    'data_as_of':datetime.now(timezone.utc).isoformat()}
        except CollectionError as error:
            raise CollectionError(str(error), [*raw, *error.evidence]) from error
        except Exception as error:
            raise CollectionError(str(error), raw) from error


DEFAULT_METHODS = {
    "catalogue": Catalogue,
    "news": News,
    "quotes": EastmoneyQuotes,
    "eastmoney_fundamentals": EastmoneyFundamentals,
    "daily": BaostockDaily,
    "sina_daily": SinaDaily,
    "fred_series": FredSeries,
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
    data.put_rows('collection_audit', [{'id': uuid4().hex,
        'settings': json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
        keys=('id',), types={'id': 'VARCHAR', 'settings': 'JSON'})
    return data.save_fact(facts)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _optional(data, identity, name):
    key = f'collect:{identity}:{name}'
    try:
        with data.transaction(readonly=True):
            return data.config(key)
    except DataError as error:
        if error.code == "missing":
            return None
        raise


def _put_config(data, identity, name, value):
    return data.save_config(f'collect:{identity}:{name}', value)


def _attachment_id(identity, name):
    return hashlib.sha256(f'{identity}/{name}'.encode()).hexdigest()


def _get_attachment(data, identity, name):
    with data.transaction(readonly=True):
        return data.object('attachment:' + _attachment_id(identity, name))


def _put_attachment(data, identity, name, content, content_type):
    from protocol import TableDef
    data.define(TableDef('attachments', {'id': 'VARCHAR', 'content_type': 'VARCHAR',
        'sha256': 'VARCHAR', 'content': 'BLOB'}, ('id',)))
    identifier = _attachment_id(identity, name)
    value = bytes(content)
    data.put_rows('attachments', [{'id': identifier, 'content_type': content_type,
        'sha256': _sha(value), 'content': value}], keys=('id',), refresh=True)
    return identifier


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
    previous = _optional(data, identity, 'manifest')
    if previous is not None:
        if previous["plan_sha256"] != report["plan_sha256"]:
            raise ValueError("capture plan changed; separate evidence directory required")
        for source in previous["sources"]:
            receipt = _optional(data, identity, source["receipt_file"])
            if _sha(_bytes(receipt)) != source["receipt_sha256"]:
                raise ValueError("receipt hash mismatch")
            if "raw_sha256" in receipt and _sha(_get_attachment(data, identity, receipt["raw_file"])) != receipt["raw_sha256"]:
                raise ValueError("raw hash mismatch")
    try:
        for path in paths:
            url = plan["origin"] + path
            request = {"url": url, "method": "GET",
                       "request_headers": {"Referer": "https://www.cninfo.com.cn/"}}
            key = _sha(_bytes(request)); receipt_name, raw_name = f'receipt:{key}', f'raw:{key}'
            receipt = _optional(data, identity, receipt_name)
            if receipt is not None:
                if receipt["request"] != request:
                    raise ValueError("request identity mismatch")
                if "raw_sha256" in receipt and _sha(_get_attachment(data, identity, raw_name)) != receipt["raw_sha256"]:
                    raise ValueError("raw hash mismatch")
            else:
                receipt = {"schema": 1, "request": request}
                try:
                    status, headers, raw = fetcher(url)
                except Exception as error:
                    receipt.update(error_type=type(error).__name__, error=str(error))
                else:
                    _put_attachment(data, identity, raw_name, raw, "application/octet-stream")
                    receipt.update(http_status=status, response_headers=headers, raw_file=raw_name,
                                   raw_sha256=_sha(raw), raw_bytes=len(raw))
                _put_config(data, identity, receipt_name, receipt)
            success = receipt.get("http_status") == 200
            report["attempted"] += 1; report["failures"] += int(not success)
            report["sources"].append({"receipt_file": receipt_name,
                                      "receipt_sha256": _sha(_bytes(_optional(data, identity, receipt_name))),
                                      "http_200": success})
        report["transport_complete"] = report["failures"] == 0
    finally:
        report["run_id"] = _sha(_bytes(report))
        _put_config(data, identity, 'manifest', report)
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
                    name = f"index:{_sha(_bytes(identity_value))}"
                    captured = _optional(data, identity, name)
                    if captured is None:
                        raw = fetcher(dict(current)).decode("utf-8")
                        payload = {"schema": 1, **identity_value, "request_sha256": _sha(_bytes(identity_value)),
                                   "raw_response_sha256": _sha(raw.encode())}
                        _put_attachment(data, identity, name, raw.encode(), 'application/json')
                        _put_config(data, identity, name, payload)
                    else:
                        payload = captured
                        raw = _get_attachment(data, identity, name).decode('utf-8')
                    if payload.get("request_sha256") != _sha(_bytes(identity_value)):
                        raise ValueError("request hash mismatch")
                    if not isinstance(raw, str) or _sha(raw.encode()) != payload.get("raw_response_sha256"):
                        raise ValueError("response hash mismatch")
                    total, more, keys = parse_page(json.loads(raw), current)
                    if expected_total is not None and total != expected_total: raise ValueError("announcement total changed within window")
                    if any(key in seen for key in keys) or len(set(keys)) != len(keys): raise ValueError("duplicate announcement identity across capture")
                    expected_total = total; seen.update(keys); report["pages"] += 1; report["records"] += len(keys)
                    report["sources"].append({"id": _attachment_id(identity, name), "sha256": payload["raw_response_sha256"], "accepted": True})
                    if not more: break
                    number += 1
                report["windows"] += 1
            except Exception as error:
                report["failures"].append({"window": window, "request": current,
                                           "error_type": type(error).__name__, "error": str(error)})
        if report["failures"]: raise ValueError("; ".join(item["error"] for item in report["failures"]))
        report["complete"] = True
    finally:
        report["run_id"] = _sha(_bytes(report)); _put_config(data, identity, 'manifest', report)
    return report
