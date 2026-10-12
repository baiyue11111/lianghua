import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock
from protocal import build_data, DataBus, DataError


def bus(directory):
    return build_data(Path(directory))


def cache(request, directory):
    return bus(directory).path("sources", "research_cache", f"{request.key}.json")


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("research_fetch_under_test", ROOT / "src" / "collect" / "factors.py")
fetch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fetch
spec.loader.exec_module(fetch)


class Result:
    error_code = "0"
    error_msg = "success"
    fields = ["code", "dividOperateDate", "foreAdjustFactor"]
    data = [["sh.600000", "2020-01-01", "1.2"]]
    code = "sh.600000"

    def __init__(self, rows=None, fail_after=None):
        self.data = rows if rows is not None else self.data
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
    def test_provider_errors_stop_without_retry_or_cache(self):
        for error in ('10002007', '10001001'):
            provider = SimpleNamespace(
                login=mock.Mock(return_value=SimpleNamespace(error_code='0')),
                logout=mock.Mock(),
                query_adjust_factor=mock.Mock(return_value=SimpleNamespace(error_code=error, error_msg='failed')))
            with self.subTest(error=error), TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {'baostock': provider}):
                status = fetch.main(['adjust', '--codes', '600000,600001', '--start', '2020-01-01',
                                     '--end', '2020-12-31'], data=bus(directory))
                self.assertEqual(status, 1)
                self.assertEqual(list(Path(directory).rglob('*.json')), [])
                provider.login.assert_called_once()
                provider.query_adjust_factor.assert_called_once()

    def test_timeout_stops_batch_instead_of_using_broken_connection(self):
        provider = SimpleNamespace(login=mock.Mock(return_value=SimpleNamespace(error_code='0')),
                                   logout=mock.Mock(), query_adjust_factor=mock.Mock(side_effect=TimeoutError('timeout')))
        with TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {'baostock': provider}):
            status = fetch.main(['adjust', '--codes', '600000,600001', '--start', '2020-01-01',
                                 '--end', '2020-12-31'], data=bus(directory))
            self.assertEqual(status, 1)
            provider.query_adjust_factor.assert_called_once()

    def test_successful_adjustment_records_request_hash_and_raw_rows(self):
        result = Result()
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=result))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        envelope = fetch.fetch_one(request, bs, timeout=5)
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["raw_response"]["rows"], result.data)
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
        bs = SimpleNamespace(query_dividend_data=mock.Mock(return_value=Result(rows=[])))
        request = fetch.Request("dividend", "600000", None, None, "2020")
        with TemporaryDirectory() as directory:
            ref = fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            path = cache(request, directory)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["status"], "empty")
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
            payload["request"] = {
                "kind": wrong.kind, "code": "sh.600000", "start": wrong.start,
                "end": wrong.end, "year": wrong.year,
            }
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
            payload["raw_response"]["rows"][0][1] = "9.9"
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
        bs = SimpleNamespace(query_adjust_factor=mock.Mock(return_value=Result()))
        request = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            ref = fetch.fetch_and_cache(request, bs, bus(directory), timeout=5)
            path = cache(request, directory)
            payload = json.loads(path.read_text())
            payload["raw_response"]["error_code"] = "10001"
            payload["raw_response_sha256"] = fetch._sha256(payload["raw_response"])
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(fetch.CacheIntegrityError):
                fetch.pending_requests([request], bus(directory))

    def test_only_pending_tasks_count_towards_limit(self):
        first = fetch.Request("adjust", "600000", "2020-01-01", "2020-12-31", None)
        second = fetch.Request("adjust", "600001", "2020-01-01", "2020-12-31", None)
        third = fetch.Request("adjust", "600002", "2020-01-01", "2020-12-31", None)
        with TemporaryDirectory() as directory:
            path = cache(first, directory)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema": 2, "status": "ok"}), encoding="utf-8")
            with mock.patch.object(fetch, "_validate_cache"):
                self.assertEqual(fetch.limit_pending([first, second, third], bus(directory), 1), [second])

    def test_codes_are_required_and_stably_deduplicated(self):
        args = SimpleNamespace(kind="adjust", codes="600001,600000,600001", codes_file=None,
                               start="2020-01-01", end="2020-12-31", year=None)
        self.assertEqual([r.code for r in fetch.build_requests(args)], ["600001", "600000"])
        args.codes = ''
        with self.assertRaises(ValueError):
            fetch.build_requests(args)

    def test_login_is_wrapped_by_query_timeout(self):
        bs = SimpleNamespace(login=mock.Mock(return_value=SimpleNamespace(
            error_code="0", error_msg="success")))
        with mock.patch.object(fetch, "_timeout", wraps=fetch._timeout) as timeout:
            fetch.login_with_timeout(bs, 7)
        timeout.assert_called_once_with(7)
