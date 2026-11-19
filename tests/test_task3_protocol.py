from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import unittest
from types import SimpleNamespace

from tests.fee_fixture import execution


@contextmanager
def native_fixture():
    from market import build_data
    from tests.history_fixture import make_snapshot

    with TemporaryDirectory() as folder:
        root = Path(folder)
        make_snapshot(root)
        with closing(build_data(root)) as data:
            yield data


def set_fixture_manifest(data, **changes):
    data.save_manifest("fixture", {**data.manifest("fixture"), **changes})


def settlement_payload():
    return {"limits": {}, "execution": execution()}


class ShiftSignalAdapter:
    @classmethod
    def validate(cls, frames, *, amount=1.0):
        from dataclasses import replace
        from protocol import HistoryBar

        output = []
        for frame in frames:
            fields = {
                symbol: {name: value + amount for name, value in values.items()}
                for symbol, values in frame.fields.items()
            }
            history = {
                symbol: tuple(HistoryBar(row.at, {
                    name: value + amount for name, value in row.fields.items()
                }) for row in rows)
                for symbol, rows in frame.history.items()
            }
            output.append(replace(frame, fields=fields, history=history))
        return tuple(output)


class ExtraHistoryAdapter:
    @classmethod
    def validate(cls, frames):
        from dataclasses import replace
        from protocol import HistoryBar

        frame = frames[0]
        symbol = next(iter(frame.history))
        rows = frame.history[symbol]
        extra = HistoryBar(rows[0].at - timedelta(days=1), dict(rows[0].fields))
        history = dict(frame.history)
        history[symbol] = (extra, *rows)
        return (replace(frame, history=history), *frames[1:])


class InvalidScaler:
    def __init__(self, mode):
        self.mode = mode

    def fit(self, values):
        return self

    def statistics(self):
        return {"center": [0.0], "scale": [1.0]}

    def transform(self, values):
        if self.mode == "short":
            return []
        return [[float("nan")] for _ in values]


class Task3ProtocolTests(unittest.TestCase):
    def test_backtest_request_uses_market_and_rejects_legacy_scope_fields(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.moving_average_cross",
            "params": {"fast_window": 2, "slow_window": 3},
            "dataset_id": "cn_a", "start": "2024-01-01", "end": "2024-01-31",
            "initial_cash": "100000",
        }
        request = parse_backtest_request({
            **base, "market": {"scope": "CN_A", "codes": ["US/AAPL", "HK/00005"],
                                "filters": [{"field": "close", "op": "gte", "value": 30}]},
        })
        self.assertEqual(dict(request.market), {
            "scope": "CN_A", "codes": ("US/AAPL", "HK/00005"),
            "filters": ({"field": "close", "op": "gte", "value": 30},),
        })
        self.assertFalse(hasattr(request, "symbols"))
        self.assertFalse(hasattr(request, "universe"))
        for field in ("symbols", "universe"):
            with self.subTest(field=field), self.assertRaises(BacktestError):
                parse_backtest_request({**base, field: ["600009"] if field == "symbols" else "CN_A"})

    def test_backtest_market_rejects_empty_scope_empty_codes_and_unknown_keys(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.moving_average_cross", "params": {},
            "dataset_id": "cn_a", "start": "2024-01-01", "end": "2024-01-31",
            "initial_cash": "100000",
        }
        for market in ({"scope": ""}, {"scope": "CN_A", "codes": []},
                       {"scope": "CN_A", "extra": True}):
            with self.subTest(market=market), self.assertRaises(BacktestError):
                parse_backtest_request({**base, "market": market})

    def test_backtest_market_rejects_nonfinite_direct_python_scalars(self):
        from decimal import Decimal
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.moving_average_cross", "params": {},
            "dataset_id": "cn_a", "start": "2024-01-01", "end": "2024-01-31",
            "initial_cash": "100000",
        }
        filters = (
            {"field": "close", "op": "eq", "value": float("nan")},
            {"field": "close", "op": "gte", "value": float("inf")},
            {"field": "roe", "op": "in", "value": [0.1, float("-inf")]},
            {"field": "roe", "op": "between", "value": [0.0, float("inf")]},
            {"field": "roe", "op": "eq", "value": Decimal("NaN")},
            {"field": "roe", "op": "in", "value": [Decimal("0.1"), Decimal("Infinity")]},
        )
        for condition in filters:
            with self.subTest(condition=condition), self.assertRaises(BacktestError):
                parse_backtest_request({**base, "market": {"scope": "CN_A",
                                                               "filters": [condition]}})

    def test_atomic_backtest_rejects_fixed_validation_and_oos_fields(self):
        from backend import KernelBacktestPort, Research
        from plugins.data import Settlement
        from protocol import BacktestError

        with native_fixture() as bus:
            port = KernelBacktestPort(bus, Settlement)
            base = {
                "run_id": "three-segments", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-01-31", "initial_cash": 1000000,
                **settlement_payload(),
            }
            for field in ("validation", "out_of_sample"):
                with self.subTest(field=field), self.assertRaises(BacktestError):
                    port.handle("backtest.run", {
                        **base, field: {"start": "2020-02-03", "end": "2020-02-04"},
                    })
            self.assertEqual(Research(bus).rows("runs"), [])
            self.assertEqual(Research(bus).rows("run_failures"), [])

    def test_data_query_declares_scope_and_bus_uses_one_reader(self):
        from unittest.mock import patch

        from market import reader
        from protocol import DataQuery

        with native_fixture() as bus:
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("close",), 20, "adjusted")
            with patch.object(reader, "_prepare_dataset", wraps=reader._prepare_dataset) as prepare:
                dataset = bus.query(query)
            prepare.assert_called_once_with(bus, query)
            self.assertEqual(dataset.dataset, "fixture")
            self.assertEqual(dataset.scope["universe"], "CN_A_SHARE")
            self.assertEqual(dataset.fields, ("close",))
            self.assertIsNone(query.symbols)

    def test_data_bus_rejects_truncated_query_and_accepts_full_coverage(self):
        from protocol import DataError, DataQuery

        with native_fixture() as bus:
            with self.assertRaisesRegex(DataError, "outside historical snapshot"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 6),
                                    ("close",), 20, "adjusted"))
            dataset = bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                          ("close",), 20, "adjusted"))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_data_bus_rejects_start_truncation_using_calendar_coverage(self):
        from protocol import DataError, DataQuery

        with native_fixture() as bus:
            first_session = date(2019, 7, 1)
            with self.assertRaisesRegex(DataError, "outside historical snapshot"):
                bus.query(DataQuery("fixture", date(2019, 6, 28), first_session,
                                    ("close",), 20, "adjusted"))

    def test_data_bus_requires_explicit_calendar_coverage(self):
        from protocol import DataError, DataQuery, Filter, TableQuery

        with native_fixture() as bus:
            bus.delete(TableQuery("calendar_coverage", (Filter("dataset", "eq", "fixture"),)))
            with self.assertRaisesRegex(DataError, "calendar coverage is required"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("close",), 20, "adjusted"))

    def test_backtest_data_request_has_no_market_scope_claim_and_market_routes_to_reader(self):
        from backend import _query
        from protocol import BacktestRequest
        from datetime import datetime
        from decimal import Decimal

        requirements = SimpleNamespace(fields=("close",), history_bars=1,
                                       price_basis="adjusted", frequency="daily")
        request = BacktestRequest("r", "s", {}, "fixture",
                                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                                  datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
                                  Decimal("100"), market={"scope": "CN_A", "codes": ["600003"]})
        query = _query(request, requirements)
        self.assertEqual(query.market["scope"], "CN_A")
        self.assertEqual(query.market["codes"], ("600003",))
        self.assertIsNone(query.symbols)

    def test_query_always_keeps_execution_prices_with_strategy_fields(self):
        from datetime import datetime
        from decimal import Decimal
        from protocol import BacktestRequest
        from backend import _query

        requirements = SimpleNamespace(fields=('signal_close',), history_bars=20,
                                       price_basis='adjusted', frequency='daily')
        request = BacktestRequest(
            'prices', 'strategy', {}, 'fixture',
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 3, tzinfo=timezone.utc), Decimal('100'),
            fields=('signal_close',),
        )

        self.assertEqual(
            _query(request, requirements).fields,
            ('open', 'high', 'low', 'close', 'volume', 'amount', 'signal_close'),
        )

    def test_settlement_failure_is_sealed_and_keeps_partial_result(self):
        from protocol import BacktestError
        from core.kernel import Kernel
        from protocol import DataQuery

        class BadStrategy:
            strategy_id = "bad"

            def requirements(self):
                return SimpleNamespace(fields=("close",), history_bars=1,
                                       price_basis="adjusted", frequency="daily")

            def generate(self, frame, snapshot):
                raise RuntimeError("decision failed")

        from tests.test_core_backtest import FakeData, FakeSettlement
        from protocol import BacktestRequest
        from decimal import Decimal
        kernel = Kernel(FakeData(), lambda _id, _params: BadStrategy(),
                        lambda req, requirements: DataQuery(
                            req.dataset_id, req.start.date(), req.end.date(),
                            requirements.fields, requirements.history_bars, requirements.price_basis,
                            market=req.market),
                        FakeSettlement)
        with self.assertRaises(BacktestError) as caught:
            kernel.run(BacktestRequest("bad", "bad", {}, "fixture",
                                       datetime(2020, 1, 1, tzinfo=timezone.utc),
                                       datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
                                       Decimal("100")))
        self.assertIsNotNone(caught.exception.partial_result)

    def test_signal_plugins_are_explicit_and_keep_execution_bars_unchanged(self):
        from protocol import DataQuery

        with native_fixture() as bus:
            set_fixture_manifest(bus, signal_plugins={
                "adapter": {"module": "plugins.data:Adapter", "method": "validate",
                            "fields": ("signal_close",)},
                "scaler": {"module": "plugins.data:Scaler", "fields": ("signal_close",), "fit_start": "2020-01-02",
                            "fit_end": "2020-01-30", "params": {"method": "standard"}},
            })
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                              20, "adjusted")
            dataset = bus.query(query)
            first = dataset.records[-1]
            self.assertEqual(first.bars[next(iter(first.bars))].open,
                             first.bars[next(iter(first.bars))].close)
            self.assertTrue(any(value["signal_close"] != 11.0
                                for value in first.frame.fields.values()))
            self.assertIn("statistics_hash", dataset.provenance["signal_plugins"]["scaler"])
            self.assertIn("statistics", dataset.provenance["signal_plugins"]["scaler"])
            from backend import KernelBacktestPort
            result = KernelBacktestPort(bus, __import__("plugins.data", fromlist=["Settlement"]).Settlement).handle(
                "backtest.run", {
                    "run_id": "real-signal", "module": "plugins.strategies.breadth_low_amount_reversal",
                    "params": {"positions": 4}, "dataset_id": "fixture",
                    "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                    **settlement_payload(),
                }).data
            self.assertEqual(result["dataset_id"], "fixture")

    def test_signal_scaler_rejects_validation_window_and_missing_training_coverage(self):
        from protocol import DataError, DataQuery

        with native_fixture() as bus:
            plugins = {
                "scaler": {"module": "plugins.data:Scaler", "fields": ("signal_close",), "fit_start": "2020-01-02",
                           "fit_end": "2020-01-31", "params": {"method": "standard"}},
            }
            set_fixture_manifest(bus, signal_plugins=plugins)
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                              20, "adjusted")
            with self.assertRaisesRegex(DataError, "end before backtest start"):
                bus.query(query)

            plugins["scaler"]["fit_end"] = "2020-01-30"
            plugins["scaler"]["fit_start"] = "2020-01-04"
            set_fixture_manifest(bus, signal_plugins=plugins)
            with self.assertRaisesRegex(DataError, "fully covered"):
                bus.query(query)

    def test_signal_scaler_rejects_short_and_nonfinite_custom_output(self):
        from protocol import DataError, DataQuery

        for mode in ("short", "nan"):
            with self.subTest(mode=mode), native_fixture() as bus:
                set_fixture_manifest(bus, signal_plugins={"scaler": {
                    "module": "tests.test_task3_protocol:InvalidScaler", "fields": ("signal_close",),
                    "fit_start": "2020-01-02", "fit_end": "2020-01-30", "params": {"mode": mode},
                }})
                with self.assertRaisesRegex(DataError, "signal scaler transform"):
                    bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                        ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                                        20, "adjusted"))

    def test_signal_plugin_replacement_is_explicit_and_merged(self):
        from protocol import DataQuery

        with native_fixture() as bus:
            set_fixture_manifest(bus, signal_plugins={
                "adapter": {
                    "module": "tests.test_task3_protocol:ShiftSignalAdapter",
                    "method": "validate", "fields": ("signal_close",),
                    "params": {"amount": 3.0},
                },
            })
            dataset = bus.query(DataQuery(
                "fixture", date(2020, 1, 31), date(2020, 2, 4),
                ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                20, "adjusted",
            ))
            frame = dataset.records[-1].frame
            symbol = next(iter(frame.fields))
            self.assertEqual(frame.fields[symbol]["signal_close"], 15.0)
            self.assertEqual(frame.fields[symbol]["close"], 12.0)
            self.assertEqual(dataset.records[-1].bars[symbol].close, 12.0)
            self.assertEqual(dataset.provenance["signal_plugins"]["adapter"]["module"],
                             "tests.test_task3_protocol:ShiftSignalAdapter")

    def test_signal_adapter_rejects_history_row_addition(self):
        from protocol import DataError, DataQuery

        with native_fixture() as bus:
            set_fixture_manifest(bus, signal_plugins={"adapter": {
                "module": "tests.test_task3_protocol:ExtraHistoryAdapter", "method": "validate",
                "fields": ("signal_close",), "params": {},
            }})
            with self.assertRaisesRegex(DataError, "history timestamps"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                                    20, "adjusted"))

    def test_failed_run_writes_evidence_without_success_manifest_and_retry_works(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort, Research
        from protocol import BacktestError

        class FailOnce(Settlement):
            calls = 0

            def apply(self, record, following, trade_start=None):
                value = super().apply(record, following, trade_start)
                if type(self).calls == 0:
                    type(self).calls += 1
                    raise RuntimeError("synthetic failure")
                return value

        with native_fixture() as bus:
            port = KernelBacktestPort(bus, FailOnce)
            payload = {"run_id": "retry", "module": "plugins.strategies.breadth_low_amount_reversal",
                       "params": {"positions": 4}, "dataset_id": "fixture",
                       "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                       **settlement_payload()}
            research = Research(bus)
            with self.assertRaisesRegex(BacktestError, "synthetic failure") as caught:
                port.handle("backtest.run", payload)
            self.assertEqual(caught.exception.evidence_ref, "lake:run_failures/retry")
            self.assertEqual(research.rows("run_failures", "retry")[0]["code"], "run_failed")
            self.assertEqual(research.rows("runs", "retry"), [])
            port.handle("backtest.run", payload)
            self.assertEqual(research.detail("retry")["status"], "succeeded")

    def test_invalid_injected_metrics_write_failure_evidence(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort, Research
        from protocol import BacktestError
        from core.calculation import Calculation

        class BadCalculation(Calculation):
            def evaluate(self, points, **parameters):
                return {"bad": float("nan")}

        with native_fixture() as bus:
            port = KernelBacktestPort(bus, Settlement)
            port._kernel._calculation = BadCalculation()
            payload = {"run_id": "bad-metrics", "module": "plugins.strategies.breadth_low_amount_reversal",
                       "params": {"positions": 4}, "dataset_id": "fixture",
                       "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                       **settlement_payload()}
            with self.assertRaises(BacktestError) as caught:
                port.handle("backtest.run", payload)
            self.assertEqual(caught.exception.code, "run_failed")
            research = Research(bus)
            failed = research.rows("run_failures", "bad-metrics")[0]
            self.assertEqual(failed["code"], "run_failed")
            self.assertIn("finite", failed["detail"])
            self.assertEqual(research.rows("runs", "bad-metrics"), [])

    def test_data_query_rejects_invalid_scope_and_history(self):
        from protocol import DataQuery

        with self.assertRaises(ValueError):
            DataQuery("", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted", date(2020, 1, 2))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 2), date(2020, 1, 1), ("close",), 1, "adjusted", date(2020, 1, 1))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 1), date(2020, 1, 2), (), 0, "adjusted", date(2020, 1, 2))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 1), date(2020, 1, 2), ("close",), True, "adjusted", date(2020, 1, 2))

    def test_data_bus_requires_reader_fields_and_history_to_match(self):
        from unittest.mock import patch

        from protocol import DataError, DataQuery, Dataset
        from protocol import MarketFrame

        day = date(2020, 1, 1)
        record = SimpleNamespace(trade_date=day, frame=MarketFrame(
            datetime(2020, 1, 1, 15, tzinfo=timezone.utc), "TEST", {}))
        query = DataQuery("fixture", day, day, ("close",), 20, "adjusted", day)
        base = dict(dataset="fixture", records=(record,), policies={"fees": object(), "tax_policy": object()},
                    provenance={"source": "test"},
                    scope={"universe": "dataset", "symbols": (), "calendar_start": day.isoformat()},
                    calendar=(day, date(2020, 1, 2)), history=20)

        with native_fixture() as bus:
            for fields, history, code in (
                (("other",), 20, "field_mismatch"),
                (("close",), 19, "history_mismatch"),
            ):
                dataset = Dataset(fields=fields, **{**base, "history": history})
                with self.subTest(fields=fields, history=history), patch(
                    "market.reader._prepare_dataset", return_value=dataset,
                ), self.assertRaises(DataError) as caught:
                    bus.query(query)
                self.assertEqual(caught.exception.code, code)

    def test_date_only_request_covers_the_entire_end_date(self):
        from protocol import parse_backtest_request

        request = parse_backtest_request({
            "run_id": "one", "module": "strategy", "params": {}, "dataset_id": "fixture",
            "start": "2020-01-01", "end": "2020-01-03", "initial_cash": "100",
        })
        self.assertEqual(request.start, datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(request.end.date(), date(2020, 1, 3))
        self.assertEqual(request.end.hour, 23)
        self.assertEqual(request.end.minute, 59)
        benchmark = parse_backtest_request({**request_data(), "benchmark_id": "hs300"})
        self.assertEqual(benchmark.benchmark_id, "hs300")
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "history": 1.5})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"lot_size": 1.5}})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"lot_size": True}})

    def test_request_rejects_unknown_fields_and_invalid_decimal_limits(self):
        from protocol import parse_backtest_request

        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limtis": {"lot_size": 10}})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"stock_weight": "not-a-number"}})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"adv_fraction": "NaN"}})

    def test_preflight_request_keeps_market_and_benchmark_inputs(self):
        from protocol import parse_backtest_request

        request = parse_backtest_request({
            **{key: value for key, value in request_data().items() if key != "run_id"},
            "market": {"scope": "CN_A", "codes": ["600036"]},
            "fields": ["signal_close"], "history": 60,
            "benchmark_id": "hs300",
        })

        self.assertEqual(request.market["codes"], ("600036",))
        self.assertEqual(request.fields, ("signal_close",))
        self.assertEqual(request.history, 60)
        self.assertEqual(request.benchmark_id, "hs300")

    def test_backend_build_uses_registered_sources_in_an_isolated_catalog(self):
        import asyncio
        import importlib.util

        from backend import build_backend
        from market import build_data

        ui = importlib.import_module("frontend.ui-protocol")
        self.assertEqual(list(inspect.signature(build_backend).parameters), ["root"])
        self.assertIsNone(importlib.util.find_spec("core.interfaces"))
        source = {"id": "example", "name": "Example", "method": "quotes",
                  "data_type": "quotes", "frequency": "snapshot", "modes": ["auto"],
                  "default": True, "defaults": {}}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(build_data(root)) as bus:
                bus.update_registry(lambda registry: {**registry, "sources": [source]})
            with closing(build_backend(root)) as backend:
                request = ui.UiRequest(
                    "sources", "collection.sources",
                    "urn:lianghua:ui:input:collection.sources:v1", {},
                )
                message, = asyncio.run(backend.send(request))
                self.assertEqual(message.kind, "collection")
                item, = message.data
                self.assertEqual(item["id"], source["id"])
                self.assertEqual(set(item), {"id", "name", "frequency", "defaults", "job",
                                            "schedule", "integrity_error", "snapshot", "records"})
                self.assertEqual(item["defaults"], {})
                self.assertEqual(item["records"], 0)

    def test_kernel_port_uses_the_explicitly_injected_settlement_plugin(self):
        from backend import KernelBacktestPort

        class Replacement:
            pass

        with native_fixture() as bus:
            port = KernelBacktestPort(bus, Replacement)
            self.assertIs(port._kernel._settlement_type, Replacement)

    def test_protocol_saves_complete_kernel_trace_through_data_bus(self):
        from plugins.data import Settlement
        from backend import KernelBacktestPort, Research

        with native_fixture() as bus:
            port = KernelBacktestPort(bus, Settlement)
            value = port.handle("backtest.run", {
                "run_id": "saved", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "evaluation": {"periods": 12, "risk_free_rate": .01},
                **settlement_payload(),
            }).data
            research = Research(bus)
            saved = research.result("saved")
            detail = research.detail("saved")
            manifest = detail["manifest"]
            self.assertEqual(detail["trades"], value["trades"])
            self.assertEqual(detail["equity"], value["equity"])
            self.assertIn("lake:runs/saved", {item["file"] for item in detail["evidence"]})
            self.assertIn("trace", value)
            self.assertIn("metrics", value)
            self.assertIn("provenance", value)
            self.assertIsNone(value["provenance"]["as_of"])
            self.assertEqual(value["provenance"]["query_semantics"], "snapshot_replay")
            self.assertEqual(saved["trace"], value["trace"])
            self.assertEqual(saved["equity"], value["equity"])
            self.assertEqual(value["effective_config"]["evaluation"], {"periods": 12, "risk_free_rate": .01})
            self.assertEqual(manifest["effective_config"], value["effective_config"])
            self.assertEqual(manifest["config_hash"], value["config_hash"])

            changed = port.handle("backtest.run", {
                "run_id": "saved-lot", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "limits": {},
                "execution": {**execution(), "check": {"model": "cn_a_share",
                    "params": {"tick": "0.01", "min_qty": 200, "step": 1}}},
            }).data
            self.assertNotEqual(value["config_hash"], changed["config_hash"])
            self.assertEqual(changed["effective_config"]["settlement"]["params"]["limits"]["lot_size"], 1)

            changed_evaluation = port.handle("backtest.run", {
                "run_id": "saved-evaluation", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "evaluation": {"periods": 252, "risk_free_rate": 0},
                **settlement_payload(),
            }).data
            self.assertEqual(changed_evaluation["equity"], value["equity"])
            self.assertEqual(changed_evaluation["trades"], value["trades"])
            self.assertNotEqual(changed_evaluation["metrics"], value["metrics"])
            self.assertNotEqual(changed_evaluation["config_hash"], value["config_hash"])


def request_data():
    return {
        "run_id": "one", "module": "strategy", "params": {}, "dataset_id": "fixture",
        "start": "2020-01-01", "end": "2020-01-03", "initial_cash": "100",
    }
