from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend import Benchmarks, KernelBacktestPort, Research, save_backtest
from market import build_data
from protocol import DataError


CODE = "sh.000300"
DAYS = ("2020-01-31", "2020-02-03", "2020-02-04")
SPEC = {
    "id": "hs300", "name": "沪深300", "source_id": "index",
    "dataset_id": "fixture", "code": CODE, "provider": "test",
    "currency": "CNY", "return_type": "price",
}


class ResearchStorageFixture(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.data = build_data(root)
        self.addCleanup(self.data.close)


class BenchmarkFixture(ResearchStorageFixture):
    def publish_prices(self, closes=(11.0, 12.0)):
        rows = [
            {"code": CODE, "date": day, "close": close}
            for day, close in zip(DAYS[:-1], closes, strict=True)
        ]
        self.data.put_rows("benchmark_prices", rows, keys=("code", "date"))
        self.data.bind("fixture", "bars", "lake:benchmark_prices")
        self.data.save_calendar("fixture", {
            "sessions": list(DAYS),
            "coverage": [{"start": DAYS[0], "end": DAYS[-1]}],
        })
        self.data.save_manifest("fixture", {
            "schema": 3,
            "calendar": "calendar:fixture",
            "inputs": {"bars": "lake:benchmark_prices"},
            "fields": {},
            "scope": {"symbols": [CODE], "universe": "fixture"},
        })

    def benchmark(self, **changes):
        return Benchmarks(self.data, [{**SPEC, **changes}])


class BenchmarkTests(BenchmarkFixture):
    def test_unbounded_benchmark_keeps_latest_native_close(self):
        self.publish_prices()
        self.data.put_rows("benchmark_prices", [
            {"code": CODE, "date": DAYS[-1], "close": 13.0},
        ], keys=("code", "date"))
        port = KernelBacktestPort(self.data, object, self.benchmark())
        result = port.handle("research.benchmark", {"id": "hs300"})
        self.assertEqual(result["end"], DAYS[-1])
        self.assertEqual(result["points"], [
            {"at": day, "close": close} for day, close in zip(DAYS, (11.0, 12.0, 13.0))
        ])
        self.assertEqual(result["source_hashes"], {"hs300": result["snapshot_id"]})
        self.assertRegex(result["snapshot_id"], "^[a-f0-9]{64}$")

    def test_compare_benchmark_does_not_require_trade_bar_fields(self):
        self.publish_prices()
        result = self.benchmark().series("hs300", DAYS[0], DAYS[1])
        self.assertEqual(result["points"], [
            {"at": DAYS[0], "close": 11.0},
            {"at": DAYS[1], "close": 12.0},
        ])

    def test_daily_benchmark_reads_the_canonical_query_not_collection_snapshot(self):
        self.publish_prices(closes=(200.0, 12.0))
        self.data.put_rows("supplier_records", [
            {"code": CODE, "date": DAYS[0], "close": 100.0},
        ], keys=("code", "date"))
        self.data.bind("supplier", "records", "lake:supplier_records")
        result = self.benchmark(source_id="supplier").series("hs300", DAYS[0], DAYS[0])
        self.assertEqual(result["points"], [{"at": DAYS[0], "close": 200.0}])

    def test_configured_index_reads_evidence_without_supplier_coupling(self):
        self.publish_prices()
        service = self.benchmark(source_id="unregistered")
        result = service.series("hs300", DAYS[0], DAYS[1])
        self.assertEqual(result["source_hashes"], {"hs300": result["snapshot_id"]})
        self.assertRegex(result["snapshot_id"], "^[a-f0-9]{64}$")
        with self.assertRaises(KeyError):
            service.series("unknown", DAYS[0], DAYS[1])

    def test_no_snapshot_and_bad_prices_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "dataset_id"):
            self.benchmark(dataset_id=None).series("hs300", DAYS[0], DAYS[1])
        self.publish_prices(closes=(-10.0, 12.0))
        with self.assertRaisesRegex(DataError, "invalid close") as error:
            self.benchmark().series("hs300", DAYS[0], DAYS[1])
        self.assertEqual(error.exception.code, "invalid_query")

    def test_requested_account_dates_must_be_fully_covered(self):
        self.publish_prices()
        with self.assertRaisesRegex(ValueError, "日期不全"):
            self.benchmark().series("hs300", DAYS[0], DAYS[1],
                                    dates=(DAYS[0], "2020-02-01", DAYS[1]))


class BenchmarkPortTests(BenchmarkFixture):
    def test_preflight_and_run_keep_config_hash_when_benchmark_is_added(self):
        from backend import _result_data
        from tests.test_core_backtest import BacktestTests, request

        kernel = BacktestTests().make_kernel()
        request_value = request(run_id="benchmark", benchmark_id="hs300")
        prepared = kernel.prepare(request_value)
        result = kernel.run(request_value)

        class Benchmark:
            def series(self, identity, start, end, *, dates):
                return {
                    "id": identity, "snapshot_id": "snapshot-hash",
                    "return_type": "price",
                    "points": [{"at": dates[0], "close": 100.0}],
                }

        value = _result_data(result, request_value, Benchmark())
        self.assertEqual(value["config_hash"], prepared.config_hash)
        self.assertNotIn("benchmark", value["effective_config"])
        self.assertEqual(value["benchmark"]["snapshot_id"], "snapshot-hash")
        self.assertEqual(value["provenance"]["benchmark"]["data_hash"], "snapshot-hash")

    def test_benchmark_protocol_port_and_missing_data(self):
        port = KernelBacktestPort(self.data, object, self.benchmark())
        self.assertEqual(port.handle("research.benchmarks", {}), [SPEC])
        request = {"id": "hs300", "start": DAYS[0], "end": DAYS[1]}
        with self.assertRaises(DataError) as error:
            port.handle("research.benchmark", request)
        self.assertEqual(error.exception.code, "missing")
        self.publish_prices()
        series = port.handle("research.benchmark", request)
        self.assertEqual(series["points"][0]["close"], 11.0)
        with self.assertRaises(ValueError):
            port.handle("research.benchmark", {**request, "start": "not-a-date"})


class ResearchEndpointTests(ResearchStorageFixture):
    def test_plot_sampling_keeps_final_observation(self):
        from tests.test_core_backtest import request

        run = request(run_id="r")
        series = [
            {"at": (date(2020, 1, 1) + timedelta(days=index)).isoformat(), "equity": 100 + index}
            for index in range(802)
        ]
        save_backtest(self.data, run, {
            "run_id": run.run_id, "strategy_id": run.strategy_id, "dataset_id": run.dataset_id,
            "first_at": series[0]["at"], "last_at": series[-1]["at"], "steps": len(series),
            "fills": 0, "final_cash": "901", "final_equity": "901", "pending_orders": 0,
            "effective_config": {}, "positions": {}, "metrics": {}, "equity": series,
        })
        research = Research(self.data)
        identity = research.list()["items"][0]["id"]
        result = research.detail(identity)
        self.assertEqual(result["equity_sampling"], {"total": 802, "stride": 2})
        self.assertEqual(result["equity"], series[::2] + [series[-1]])
