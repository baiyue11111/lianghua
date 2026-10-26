import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock
from market import build_data, DataBus
from protocol import DataError


def bus(directory):
    return build_data(Path(directory))


def cache(request, directory):
    return bus(directory).path("sources", "facts", f"{request.key}.json")


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("research_fetch_under_test", ROOT / "src" / "collect" / "methods.py")
fetch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fetch
spec.loader.exec_module(fetch)


class Result:
    error_code = "0"
    error_msg = "success"
    fields = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor"]
    data = [["sh.600000", "2020-01-01", "1.2", "1.1"]]
    code = "sh.600000"

    def __init__(self, rows=None, fail_after=None, fields=None):
        self.data = rows if rows is not None else self.data
        self.fields = fields if fields is not None else type(self).fields
        self.index = 0
        self.fail_after = fail_after

    def next(self):
        if self.fail_after is not None and self.index >= self.fail_after:
            raise RuntimeError("truncated response")
        return self.index < len(self.data)

    def get_row_data(self):
        row = self.data[self.index]
        self.index += 1
        return row


class ResearchFetchTests(TestCase):
    def dividend_base(self):
        return {"schema": 1, "kind": "dividend", "code": "sh.600000",
                "start": None, "end": None, "year": "2014", "source_hash": "a"*64,
                "records": [{"announce_date": "2014-03-01", "registration_date": "2014-06-01",
                  "operation_date": "2014-06-02", "payment_date": "2014-06-03",
                  "stock_market_date": None, "cash_per_share": 0.5,
                  "stock_dividend_per_share": 0, "reserve_to_stock_per_share": None,
                  "cash_after_tax_text": None}]}

    def dividend_html(self, *, reserve="0", day="2014-06-02", duplicate=False):
        row=f"<tr><td>0</td><td>{reserve}</td><td>7.25</td><td>实施</td><td>{day}</td><td>2014-06-01</td><td>--</td></tr>"
        return ("<table><thead><tr><th colspan=7>分红方案(每10股)</th></tr><tr><th>送股(股)</th><th>转增(股)</th><th>派息(税前)(元)</th>"
                "<th>进度</th><th>除权除息日</th><th>股权登记日</th><th>红股上市日</th></tr></thead>"
                +row+(row if duplicate else "")+"</table>")

    def test_dividend_adapter_uses_explicit_public_market_quantities(self):
        base=self.dividend_base()
        fact=fetch.DividendHistory.normalize(base,self.dividend_html())
        self.assertEqual(fact["records"][0]["reserve_to_stock_per_share"],0)
        self.assertEqual(fact["records"][0]["cash_per_share"],0.725)
        self.assertEqual(fact["records"][0]["payment_date"],"2014-06-03")
        self.assertIsNone(base["records"][0]["reserve_to_stock_per_share"])
        self.assertNotEqual(fact["source_hash"],base["source_hash"])

    def test_dividend_events_follow_final_implemented_history(self):
        from copy import deepcopy
        base = self.dividend_base()
        old = deepcopy(base['records'][0])
        old.update(registration_date='2014-05-01', operation_date='2014-05-02', payment_date='2014-05-03')
        base['records'].insert(0, old)
        result = fetch.DividendHistory.normalize(base, self.dividend_html())
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['operation_date'], '2014-06-02')
        self.assertEqual(len(base['records']), 2)
        extra = self.dividend_html().replace('</table>',
            '<tr><td>0</td><td>0</td><td>1</td><td>实施</td><td>2014-07-02</td><td>2014-07-01</td><td>--</td></tr></table>')
        with self.assertRaises(ValueError):
            fetch.DividendHistory.normalize(base, extra)

    def test_dividend_history_excludes_events_after_requested_end(self):
        base = self.dividend_base()
        extra = self.dividend_html().replace('</table>',
            '<tr><td>0</td><td>0</td><td>1</td><td>实施</td><td>2014-07-02</td><td>2014-07-01</td><td>--</td></tr></table>')
        result = fetch.DividendHistory.normalize(base, extra, end='2014-06-30')
        self.assertEqual(len(result['records']), 1)
        with self.assertRaises(ValueError):
            fetch.DividendHistory.normalize(base, extra, end='2014-07-03')

    def test_dividend_detail_supplies_a_missing_implemented_event(self):
        html = ('<table><thead><tr><th colspan="8">分红方案(每10股)</th></tr>'
                '<tr><th>公告日期</th><th>送股(股)</th><th>转增(股)</th><th>派息(税前)(元)</th>'
                '<th>进度</th><th>除权除息日</th><th>股权登记日</th><th>红股上市日</th></tr></thead>'
                '<tr><td>2014-06-01</td><td>0</td><td>0</td><td>2</td><td>实施</td>'
                '<td>2014-06-12</td><td>2014-06-11</td><td>--</td></tr></table>')
        detail = ('<title>600000 分红明细</title><table>'
                  '<tr><td>登记日</td><td>2014-06-11</td></tr><tr><td>除息日</td><td>2014-06-12</td></tr>'
                  '<tr><td>红利/配股起始日（送、转股到账日）</td><td>2014-06-16</td></tr>'
                  '<tr><td>上市日</td><td>--</td></tr></table>')
        base = self.dividend_base()
        base['records'] = []
        result = fetch.DividendHistory.normalize(base, html, load=lambda url: detail)
        self.assertEqual(result['records'][0]['cash_per_share'], .2)
        self.assertEqual(result['records'][0]['payment_date'], '2014-06-16')
        with self.assertRaises(ValueError):
            fetch.DividendHistory.normalize(base, html, load=lambda url: detail.replace('2014-06-11','2014-06-10'))

    def test_dividend_duplicate_rows_merge_only_consistent_fields(self):
        from copy import deepcopy
        base = self.dividend_base()
        duplicate = deepcopy(base['records'][0])
        duplicate['cash_per_share'] = None
        base['records'].append(duplicate)
        result = fetch.DividendHistory.normalize(base, self.dividend_html())
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['payment_date'], '2014-06-03')
        base['records'][1]['payment_date'] = '2014-06-04'
        with self.assertRaises(DataError):
            fetch.DividendHistory.normalize(base, self.dividend_html())

    def test_dividend_empty_optional_text_is_missing_not_a_conflict(self):
        from copy import deepcopy
        base = self.dividend_base()
        base['records'][0]['cash_after_tax_text'] = ''
        duplicate = deepcopy(base['records'][0])
        duplicate['cash_after_tax_text'] = '0.45或0.5'
        base['records'].append(duplicate)
        result = fetch.DividendHistory.normalize(base, self.dividend_html())
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['cash_after_tax_text'], '0.45或0.5')
        self.assertEqual(base['records'][0]['cash_after_tax_text'], '')

    def test_dividend_tax_descriptions_are_evidence_not_gross_terms(self):
        from copy import deepcopy
        base = self.dividend_base()
        base['records'][0]['cash_after_tax_text'] = '0.45或0.5'
        row = deepcopy(base['records'][0])
        row['cash_after_tax_text'] = '0.4或0.5'
        base['records'].append(row)
        result = fetch.DividendHistory.normalize(base, self.dividend_html())
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['cash_after_tax_text'], '0.45或0.5 | 0.4或0.5')
        row['cash_per_share'] = .6
        with self.assertRaises(DataError):
            fetch.DividendHistory.normalize(base, self.dividend_html())

    def test_dividend_proposal_does_not_become_paid_income(self):
        html = self.dividend_html().replace('colspan=7', 'colspan=8').replace(
            '<th>送股(股)</th>', '<th>公告日期</th><th>送股(股)</th>').replace(
            '<tr><td>0</td>', '<tr><td>2014-03-01</td><td>0</td>').replace(
            '实施', '预案').replace('2014-06-01', '--').replace('2014-06-02', '--')
        base = self.dividend_base()
        self.assertEqual(fetch.DividendHistory.normalize(base, html)['records'], [])
        with self.assertRaises(ValueError):
            fetch.DividendHistory.normalize(base, html.replace('2014-03-01', '2014-03-02'))

    def test_dividend_duplicate_rates_require_the_public_market_rate(self):
        from copy import deepcopy
        base = self.dividend_base()
        duplicate = deepcopy(base['records'][0])
        duplicate['cash_per_share'] = .725
        base['records'].append(duplicate)
        result = fetch.DividendHistory.normalize(base, self.dividend_html())
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['cash_per_share'], .725)
        base['records'][1]['payment_date'] = '2014-06-04'
        with self.assertRaises(DataError):
            fetch.DividendHistory.normalize(base, self.dividend_html())

    def test_dividend_adapter_rejects_missing_quantities_and_ambiguous_dates(self):
        for html in (self.dividend_html(reserve="--"), self.dividend_html(day="2014-06-03"),
                     self.dividend_html(duplicate=True), self.dividend_html().replace("每10股","每股")):
            with self.subTest(html=html), self.assertRaises((ValueError,fetch.CacheIntegrityError)):
                fetch.DividendHistory.normalize(self.dividend_base(),html)

    def test_stock_basic_raw_becomes_security_fact_and_round_trips(self):
        fields = ["code", "ipoDate", "outDate", "type", "status"]
        raw = {"error_code": "0", "error_msg": "success", "fields": fields,
               "rows": [["sz.301116", "2022-01-18", "", "1", "1"]]}
        payload = {"schema": 1, "request": "query_stock_basic(sz.301116)",
                   "raw_response": raw, "raw_response_sha256": fetch._sha256(raw)}
        facts = fetch.normalize_facts(payload)
        self.assertEqual(facts["kind"], "security")
        self.assertEqual(facts["records"], [{"listing_date": "2022-01-18", "delisting_date": None,
                                            "type": "1", "status": "1"}])
        with TemporaryDirectory() as directory:
            ref = fetch.write_stock_basic(bus(directory), payload)
            saved = json.loads(bus(directory).read(ref.kind, ref.identity, ref.name))
            self.assertEqual(saved, facts)
            self.assertEqual(ref.identity, "facts")
            self.assertEqual(ref.name, "stock_basic_sz.301116.json")

    def test_stock_basic_adapter_rejects_changed_raw_and_wrong_code(self):
        fields = ["code", "ipoDate", "outDate", "type", "status"]
        raw = {"error_code": "0", "error_msg": "success", "fields": fields,
               "rows": [["sz.301116", "2022-01-18", "", "1", "1"]]}
        payload = {"schema": 1, "request": "query_stock_basic(sz.301116)",
                   "raw_response": raw, "raw_response_sha256": fetch._sha256(raw)}
        broken = dict(payload, raw_response_sha256="0" * 64)
        with self.assertRaises(fetch.CacheIntegrityError):
            fetch.normalize_facts(broken)
        raw["rows"][0][0] = "sz.301117"
        payload["raw_response_sha256"] = fetch._sha256(raw)
        with self.assertRaises(fetch.CacheIntegrityError):
            fetch.normalize_facts(payload)

    def test_adjust_facts_are_written_and_read_from_standard_store(self):
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        provider = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=Result()))
        with TemporaryDirectory() as directory:
            fetch.fetch_and_cache(request, provider, bus(directory), timeout=5)
            path = cache(request, directory)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["records"], [{"date": "2020-01-01", "forward": 1.2, "backward": 1.1}])
            self.assertEqual(fetch.pending_requests([request], bus(directory)), [])

    def test_provider_errors_stop_without_retry_or_cache(self):
        for error in ('10002007', '10001001'):
            provider = SimpleNamespace(
                login=mock.Mock(return_value=SimpleNamespace(error_code='0')),
                logout=mock.Mock(),
                query_adjust_factor=mock.Mock(return_value=SimpleNamespace(error_code=error, error_msg='failed')))
            with self.subTest(error=error), TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {'baostock': provider}):
                with self.assertRaises(fetch.ProviderError):
                    fetch.fetch_and_cache(fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None), provider, bus(directory))
                self.assertEqual(list(Path(directory).rglob('*.json')), [])
                provider.query_adjust_factor.assert_called_once()

    def test_timeout_stops_batch_instead_of_using_broken_connection(self):
        provider = SimpleNamespace(login=mock.Mock(return_value=SimpleNamespace(error_code='0')),
                                   logout=mock.Mock(), query_adjust_factor=mock.Mock(side_effect=TimeoutError('timeout')))
        with TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {'baostock': provider}):
            with self.assertRaises(TimeoutError):
                fetch.fetch_and_cache(fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None), provider, bus(directory))
            provider.query_adjust_factor.assert_called_once()

    def test_successful_adjustment_normalizes_raw_rows(self):
        result = Result()
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=result))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        envelope = fetch.fetch_one(request, bs, timeout=5)
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(fetch.normalize_facts(envelope)["records"], [
            {"date": "2020-01-01", "forward": 1.2, "backward": 1.1}])
        self.assertEqual(len(envelope["request_fields_sha256"]), 64)
        bs.query_adjust_factor.assert_called_once_with(
            code="sh.600000", start_date="2020-01-01", end_date="2020-12-31")

    def test_provider_error_does_not_write_cache(self):
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=SimpleNamespace(
            error_code="10001", error_msg="bad request")))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            with self.assertRaises(fetch.ProviderError):
                fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_midstream_error_does_not_write_partial_cache(self):
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=Result(fail_after=0)))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_successful_empty_response_is_distinct_and_resumable(self):
        fields = ["code", "dividPlanAnnounceDate", "dividRegistDate", "dividOperateDate",
                  "dividPayDate", "dividStockMarketDate", "dividCashPsBeforeTax", "dividStocksPs",
                  "dividReserveToStockPs", "dividCashPsAfterTax"]
        bs = SimpleNamespace(query_dividend_data=mock.Mock(return_value=Result(rows=[], fields=fields)))
        request = fetch.Request("dividend", "600000", None, None, "2020")
        with TemporaryDirectory() as directory:
            ref = fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            path = cache(request, directory)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["kind"], "dividend")
            self.assertEqual(payload["records"], [])
            self.assertEqual(fetch.pending_requests([request], bus(directory)), [])
            bs.query_dividend_data.assert_called_once_with(
                code="sh.600000", year="2020", yearType="operate")

    def test_cache_with_different_request_is_rejected(self):
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=Result()))
        original = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        wrong = fetch.Request("adjust", "600000", "2021-01-01", "2021-12-31", None)
        with TemporaryDirectory() as directory:
            fetch.fetch_and_cache(original, bs, bus(directory), timeout=5)
            path = cache(original, directory)
            payload = json.loads(path.read_text())
            payload["start"] = wrong.start
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(fetch.CacheIntegrityError):
                fetch.pending_requests([original], bus(directory))

    def test_tampered_response_is_rejected_even_when_status_is_ok(self):
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=Result()))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            ref = fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            path = cache(request, directory)
            payload = json.loads(path.read_text())
            payload["records"][0]["forward"] = -9.9
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(fetch.CacheIntegrityError):
                fetch.pending_requests([request], bus(directory))

    def test_unsupported_market_is_rejected_instead_of_mapped_to_sz(self):
        with self.assertRaises(fetch.UnsupportedMarketError):
            fetch._bs_code("8xxxxx")
        with self.assertRaises(fetch.UnsupportedMarketError):
            fetch._bs_code("sh.000001")
        self.assertEqual(fetch._bs_code("302132"), "sz.302132")

    def test_corrupt_cache_is_not_treated_as_missing(self):
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            path = cache(request, directory)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(fetch.CacheIntegrityError):
                fetch.pending_requests([request], bus(directory))

    def test_cache_read_oserror_is_a_integrity_failure(self):
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            path = cache(request, directory)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(DataBus, "read", side_effect=DataError("io_failed", "denied")):
                with self.assertRaises(fetch.CacheIntegrityError):
                    fetch.pending_requests([request], bus(directory))

    def test_nonzero_raw_error_code_is_rejected(self):
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        raw = {"error_code": "10001", "error_msg": "bad", "code": "sh.600000",
               "fields": Result.fields, "rows": Result.data}
        request_payload = {"kind": request.kind, "code": "sh.600000", "start": request.start,
                           "end": request.end, "year": request.year}
        payload = {"schema": 2, "request": request_payload, "request_sha256": fetch._sha256(request_payload),
                   "request_fields_sha256": fetch.hashlib.sha256(",".join(Result.fields).encode()).hexdigest(),
                   "raw_response": raw, "raw_response_sha256": fetch._sha256(raw), "status": "ok"}
        with self.assertRaises(fetch.CacheIntegrityError):
            fetch.normalize_facts(payload)

    def test_only_pending_tasks_count_towards_limit(self):
        first = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        second = fetch.Request("adjust", "600001", "2020-01-01", "2020-12-31", None)
        third = fetch.Request("adjust", "600002", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            path = cache(first, directory)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema": 1, "kind": "adjust"}), encoding="utf-8")
            with mock.patch.object(fetch, "_validate_cache"):
                self.assertEqual(fetch.limit_pending([first, second, third], bus(directory), 1), [second])

    def test_request_identity_uses_standard_market_code(self):
        plain = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        qualified = fetch.Request("adjust", "sh.600000", "2020-01-01", "2020-12-31", None)
        self.assertEqual(plain.key, qualified.key)
        self.assertFalse(hasattr(fetch, "main"))

    def test_login_is_wrapped_by_query_timeout(self):
        bs = SimpleNamespace(login=mock.Mock(return_value=SimpleNamespace(
            error_code="0", error_msg="success")))
        with mock.patch.object(fetch, "_timeout", wraps=fetch._timeout) as timeout:
            fetch.login_with_timeout(bs, 7)
        timeout.assert_called_once_with(7)
