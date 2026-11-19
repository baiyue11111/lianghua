from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock
from collect import methods as fetch
from market import build_data, fact_ref
from protocol import DataError


def adjustment_envelope(*, error_code="0"):
    fields = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor"]
    request = {"kind": "adjust", "code": "sh.600000", "start": "2020-01-01",
               "end": "2020-12-31", "year": None}
    raw = {"error_code": error_code, "error_msg": "",
           "code": request["code"], "fields": fields,
           "rows": [[request["code"], "2020-01-01", "1.2", "1.1"]]}
    return {"schema": 2, "status": "ok", "request": request,
            "request_sha256": fetch._sha256(request),
            "request_fields_sha256": sha256(",".join(fields).encode()).hexdigest(),
            "raw_response": raw, "raw_response_sha256": fetch._sha256(raw)}


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

    def test_adjustment_envelope_normalizes_raw_rows(self):
        raw = adjustment_envelope()
        facts = fetch.normalize_facts(raw)
        self.assertEqual(facts["code"], "sh.600000")
        self.assertEqual(facts["records"], [
            {"date": "2020-01-01", "forward": 1.2, "backward": 1.1},
        ])
        self.assertEqual(facts["source_hash"], raw["raw_response_sha256"])

    def test_tampered_response_is_rejected_even_when_status_is_ok(self):
        raw = adjustment_envelope()
        raw["raw_response"]["rows"][0][2] = "-9.9"
        with self.assertRaisesRegex(fetch.CacheIntegrityError, "raw response hash mismatch"):
            fetch.normalize_facts(raw)

    def test_nonzero_raw_error_code_is_rejected(self):
        raw = adjustment_envelope(error_code="10001")
        with self.assertRaisesRegex(fetch.CacheIntegrityError, "response contains an error"):
            fetch.normalize_facts(raw)

    def test_corrupt_fact_coverage_is_not_treated_as_missing(self):
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            facts = fetch.normalize_facts(adjustment_envelope())
            ref = data.save_fact(facts)
            self.assertEqual(data.fact(ref), facts)
            missing = fact_ref("adjust", "sh.600001", facts["start"], facts["end"])
            with self.assertRaises(DataError) as error:
                data.fact(missing)
            self.assertEqual(error.exception.code, "missing")

            data.put_rows("fact_coverage", [{"reference": ref, "record_count": 2}],
                          keys=("reference",), refresh=True)
            with self.assertRaisesRegex(DataError, "row count differs") as error:
                data.fact(ref)
            self.assertEqual(error.exception.code, "data_corrupt")

    def test_stock_basic_provider_error_stops_without_retry(self):
        for code in ("10001", "10002007"):
            provider = SimpleNamespace(query_stock_basic=mock.Mock(return_value=SimpleNamespace(
                error_code=code, error_msg="failed")))
            with self.subTest(code=code), self.assertRaisesRegex(fetch.ProviderError, code):
                fetch.collect_stock_basic(provider, timeout=5, code="600000")
            provider.query_stock_basic.assert_called_once_with(code="sh.600000")

    def test_stock_basic_iteration_error_does_not_return_partial_evidence(self):
        response = SimpleNamespace(
            error_code="0", error_msg="success", fields=["code", "ipoDate", "outDate", "type", "status"],
            next=mock.Mock(side_effect=[True, TimeoutError("truncated response")]),
            get_row_data=mock.Mock(return_value=["sh.600000", "1999-11-10", "", "1", "1"]),
        )
        provider = SimpleNamespace(query_stock_basic=mock.Mock(return_value=response))
        with self.assertRaisesRegex(TimeoutError, "truncated response"):
            fetch.collect_stock_basic(provider, timeout=5, code="600000")
        response.get_row_data.assert_called_once()
        provider.query_stock_basic.assert_called_once_with(code="sh.600000")

    def test_unsupported_market_is_rejected_instead_of_mapped_to_sz(self):
        provider = SimpleNamespace(query_stock_basic=mock.Mock())
        for code in ("8xxxxx", "sh.000001"):
            with self.subTest(code=code), self.assertRaises(fetch.UnsupportedMarketError):
                fetch.collect_stock_basic(provider, timeout=5, code=code)
        provider.query_stock_basic.assert_not_called()

    def test_login_is_wrapped_by_query_timeout(self):
        provider = SimpleNamespace(login=mock.Mock(return_value=SimpleNamespace(
            error_code="0", error_msg="success")))
        with mock.patch.object(fetch, "_timeout", wraps=fetch._timeout) as timeout:
            fetch.login_with_timeout(provider, 7)
        timeout.assert_called_once_with(7)
