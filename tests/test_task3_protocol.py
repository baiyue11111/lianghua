from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import unittest
from types import SimpleNamespace

from tests.fee_fixture import execution


class ConfigSettlement:
    def __init__(self, initial_cash, fees, tax_policy, *, limits=None, mode="default"):
        self.initial_cash = initial_cash
        self.limits = limits
        self.mode = mode


def settlement_payload():
    return {"limits": {"lot_size": 100}, "execution": execution()}


class ShiftSignalAdapter:
    @classmethod
    def validate(cls, frames, *, amount=1.0):
        from dataclasses import replace
        from protocal import HistoryBar

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
        from protocal import HistoryBar

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
    def test_data_query_declares_scope_and_bus_uses_one_reader(self):
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            bus = DataBus(DataStore(root))
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("close",), 20, "adjusted")
            dataset = bus.query(query)
            self.assertEqual(dataset.dataset, "fixture")
            self.assertEqual(dataset.scope["universe"], "CN_A_SHARE")
            self.assertEqual(dataset.fields, ("close",))

        query = DataQuery(
            dataset="fixture", start=date(2020, 1, 31), end=date(2020, 2, 4),
            fields=("close",), history=20, price_basis="adjusted", symbols=None,
        )
        self.assertIsNone(query.symbols)

    def test_data_bus_rejects_truncated_query_and_accepts_full_coverage(self):
        from protocal import DataBus, DataError, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            bus = DataBus(DataStore(root))
            with self.assertRaisesRegex(DataError, "outside historical snapshot"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 6),
                                    ("close",), 20, "adjusted"))
            dataset = bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                          ("close",), 20, "adjusted"))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_data_bus_rejects_start_truncation_using_calendar_coverage(self):
        from protocal import DataBus, DataError, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            bus = DataBus(DataStore(root))
            first_session = date(2019, 7, 1)
            with self.assertRaisesRegex(DataError, "outside historical snapshot"):
                bus.query(DataQuery("fixture", date(2019, 6, 28), first_session,
                                    ("close",), 20, "adjusted"))

    def test_data_bus_requires_explicit_calendar_coverage(self):
        import json
        from protocal import DataBus, DataError, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(path.read_text())
            del manifest["calendar"]
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DataError, "calendar"):
                DataBus(DataStore(root)).query(
                    DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("close",), 20, "adjusted")
                )

    def test_query_cannot_lower_strategy_universe_or_claim_unproven_pit(self):
        from protocal import _query, DataQuery, DataBus
        from protocal import BacktestRequest
        from datetime import datetime
        from decimal import Decimal
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        requirements = SimpleNamespace(fields=("close",), history_bars=1,
                                       price_basis="adjusted", symbols=None,
                                       universe="CN_A_SHARE", frequency="daily")
        request = BacktestRequest("r", "s", {}, "fixture",
                                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                                  datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
                                  Decimal("100"), universe="dataset")
        lowered = BacktestRequest("r", "s", {}, "fixture",
                                  request.start, request.end, Decimal("100"), universe="OTHER")
        self.assertEqual(_query(request, requirements).universe, "CN_A_SHARE")
        with self.assertRaises(Exception):
            _query(lowered, requirements)
        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                              20, "adjusted", date(2020, 2, 5), universe="CN_A_SHARE")
            with self.assertRaises(Exception):
                bus.query(query)

    def test_query_always_keeps_execution_prices_with_strategy_fields(self):
        from datetime import datetime
        from decimal import Decimal
        from protocal import BacktestRequest
        from protocal import _query

        requirements = SimpleNamespace(fields=('signal_close',), history_bars=20,
                                       price_basis='adjusted', symbols=None,
                                       universe='dataset', frequency='daily')
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

    def test_pit_requires_calendar_file_binding_not_only_calendar_source(self):
        import json
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            manifest_path = root / "data" / "source" / "fixture" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["source_availability"] = [
                {"file": item["file"], "sha256": item["sha256"], "available_at": "2020-02-05"}
                for item in manifest["files"] if item["file"] != "calendar.json"
            ]
            manifest["files"] = [item for item in manifest["files"] if item["file"] != "calendar.json"]
            manifest_path.write_text(json.dumps(manifest))
            bus = DataBus(DataStore(root))
            with self.assertRaisesRegex(Exception, "unbound historical evidence|availability"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                                    20, "adjusted", date(2020, 2, 5), universe="CN_A_SHARE"))

    def test_settlement_failure_is_sealed_and_keeps_partial_result(self):
        from protocal import BacktestError
        from core.kernel import Kernel
        from protocal import DataQuery

        class BadStrategy:
            strategy_id = "bad"

            def requirements(self):
                return SimpleNamespace(fields=("close",), history_bars=1, price_basis="adjusted",
                                       symbols=None, universe="fixture", frequency="daily")

            def generate(self, frame, snapshot):
                raise RuntimeError("decision failed")

        from tests.test_core_backtest import FakeData, FakeSettlement
        from protocal import BacktestRequest
        from decimal import Decimal
        kernel = Kernel(FakeData(), lambda _id, _params: BadStrategy(),
                        lambda req, requirements: DataQuery(
                            req.dataset_id, req.start.date(), req.end.date(),
                            requirements.fields, requirements.history_bars, requirements.price_basis,
                        None, requirements.symbols, requirements.universe, requirements.frequency),
                        FakeSettlement)
        with self.assertRaises(BacktestError) as caught:
            kernel.run(BacktestRequest("bad", "bad", {}, "fixture",
                                       datetime(2020, 1, 1, tzinfo=timezone.utc),
                                       datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
                                       Decimal("100")))
        self.assertIsNotNone(caught.exception.partial_result)

    def test_result_and_research_use_saved_json_trace_contract(self):
        from protocal import DataBus
        from protocal import BacktestRequest, Research, save_backtest
        from storage import DataStore
        from decimal import Decimal

        with TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            request_value = BacktestRequest(
                "json-run", "fake", {}, "fixture", datetime(2020, 1, 1, tzinfo=timezone.utc),
                datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc), Decimal("100"))
            save_backtest(bus, request_value, {
                "id": "json-run", "run_id": "json-run", "strategy_id": "fake",
                "dataset_id": "fixture", "parameters": {}, "effective_config": {},
                "config_hash": "hash", "metrics": {"final_equity": 100},
                "equity": [{"at": "2020-01-01", "equity": 100}],
                "trades": [{"id": "fill"}], "trace": {"frames": []},
                "provenance": {}, "final_cash": "100", "final_equity": "100",
                "steps": 1, "fills": 1, "pending_orders": 0, "positions": {},
            })
            detail = Research(bus).detail("json-run")
            self.assertEqual(detail["trades"], [{"id": "fill"}])
            self.assertEqual(detail["equity"][-1]["equity"], 100)
            self.assertTrue(any(item["file"].endswith("result.json") for item in detail["evidence"]))

    def test_signal_plugins_are_explicit_and_keep_execution_bars_unchanged(self):
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            update_manifest(root, signal_plugins={
                "adapter": {"module": "plugins.data:Adapter", "method": "validate",
                            "fields": ("signal_close",)},
                "scaler": {"module": "plugins.data:Scaler", "fields": ("signal_close",), "fit_start": "2020-01-02",
                            "fit_end": "2020-01-30", "params": {"method": "standard"}},
            })
            bus = DataBus(DataStore(root))
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
            from protocal import KernelBacktestPort
            result = KernelBacktestPort(bus, __import__("plugins.data", fromlist=["Settlement"]).Settlement).handle(
                "workbench.backtest", {
                    "run_id": "real-signal", "module": "plugins.strategies.breadth_low_amount_reversal",
                    "params": {"positions": 4}, "dataset_id": "fixture",
                    "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                    **settlement_payload(),
                }).data
            self.assertEqual(result["dataset_id"], "fixture")

    def test_signal_scaler_rejects_validation_window_and_missing_training_coverage(self):
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            plugins = {
                "scaler": {"module": "plugins.data:Scaler", "fields": ("signal_close",), "fit_start": "2020-01-02",
                           "fit_end": "2020-01-31", "params": {"method": "standard"}},
            }
            update_manifest(root, signal_plugins=plugins)
            bus = DataBus(DataStore(root))
            query = DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                              ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                              20, "adjusted")
            with self.assertRaisesRegex(ValueError, "end before backtest start"):
                bus.query(query)

            plugins["scaler"]["fit_end"] = "2020-01-30"
            plugins["scaler"]["fit_start"] = "2020-01-04"
            update_manifest(root, signal_plugins=plugins)
            with self.assertRaisesRegex(ValueError, "fully covered"):
                bus.query(query)

    def test_signal_scaler_rejects_short_and_nonfinite_custom_output(self):
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        for mode in ("short", "nan"):
            with self.subTest(mode=mode), TemporaryDirectory() as root:
                root = Path(root)
                make_snapshot(root)
                update_manifest(root, signal_plugins={"scaler": {
                    "module": "tests.test_task3_protocol:InvalidScaler", "fields": ("signal_close",),
                    "fit_start": "2020-01-02", "fit_end": "2020-01-30", "params": {"mode": mode},
                }})
                bus = DataBus(DataStore(root))
                with self.assertRaisesRegex(ValueError, "signal scaler transform"):
                    bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                        ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                                        20, "adjusted"))

    def test_signal_plugin_replacement_is_explicit_and_merged(self):
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            update_manifest(root, signal_plugins={
                "adapter": {
                    "module": "tests.test_task3_protocol:ShiftSignalAdapter",
                    "method": "validate", "fields": ("signal_close",),
                    "params": {"amount": 3.0},
                },
            })
            bus = DataBus(DataStore(root))
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
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot, update_manifest

        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            update_manifest(root, signal_plugins={"adapter": {
                "module": "tests.test_task3_protocol:ExtraHistoryAdapter", "method": "validate",
                "fields": ("signal_close",), "params": {},
            }})
            bus = DataBus(DataStore(root))
            with self.assertRaisesRegex(ValueError, "history timestamps"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                                    20, "adjusted"))

    def test_failed_run_writes_evidence_without_success_manifest_and_retry_works(self):
        from plugins.data import Settlement
        from protocal import DataBus, KernelBacktestPort
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        class FailOnce(Settlement):
            calls = 0

            def apply(self, record, following, trade_start=None):
                value = super().apply(record, following, trade_start)
                if type(self).calls == 0:
                    type(self).calls += 1
                    raise RuntimeError("synthetic failure")
                return value

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, FailOnce)
            payload = {"run_id": "retry", "module": "plugins.strategies.breadth_low_amount_reversal",
                       "params": {"positions": 4}, "dataset_id": "fixture",
                       "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                       **settlement_payload()}
            with self.assertRaises(Exception):
                port.handle("workbench.backtest", payload)
            self.assertTrue(bus.path("runs", "retry", "failed.json").is_file())
            self.assertFalse(bus.path("runs", "retry", "manifest.json").exists())
            port.handle("workbench.backtest", payload)
            self.assertTrue(bus.path("runs", "retry", "manifest.json").is_file())

    def test_invalid_injected_metrics_write_failure_evidence(self):
        import json
        from plugins.data import Settlement
        from protocal import DataBus, KernelBacktestPort
        from storage import DataStore
        from tests.history_fixture import make_snapshot
        from core.calculation import Calculation

        class BadCalculation(Calculation):
            def evaluate(self, points, **parameters):
                return {"bad": float("nan")}

        with TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, Settlement)
            port._kernel._calculation = BadCalculation()
            payload = {"run_id": "bad-metrics", "module": "plugins.strategies.breadth_low_amount_reversal",
                       "params": {"positions": 4}, "dataset_id": "fixture",
                       "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                       **settlement_payload()}
            with self.assertRaises(Exception) as caught:
                port.handle("workbench.backtest", payload)
            self.assertEqual(caught.exception.code, "run_failed")
            failed = json.loads(bus.read("runs", "bad-metrics", "failed.json"))
            self.assertEqual(failed["code"], "run_failed")
            self.assertFalse(bus.path("runs", "bad-metrics", "manifest.json").exists())

    def test_data_query_rejects_invalid_scope_and_history(self):
        from protocal import DataQuery

        with self.assertRaises(ValueError):
            DataQuery("", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted", date(2020, 1, 2))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 2), date(2020, 1, 1), ("close",), 1, "adjusted", date(2020, 1, 1))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 1), date(2020, 1, 2), (), 0, "adjusted", date(2020, 1, 2))
        with self.assertRaises(ValueError):
            DataQuery("fixture", date(2020, 1, 1), date(2020, 1, 2), ("close",), True, "adjusted", date(2020, 1, 2))

    def test_data_bus_requires_reader_fields_and_history_to_match(self):
        from protocal import DataBus, DataQuery, Dataset
        from protocal import MarketFrame

        day = date(2020, 1, 1)
        record = SimpleNamespace(trade_date=day, frame=MarketFrame(
            datetime(2020, 1, 1, 15, tzinfo=timezone.utc), "TEST", {}))
        query = DataQuery("fixture", day, day, ("close",), 20, "adjusted", day)
        base = dict(dataset="fixture", records=(record,), policies={"fees": object(), "tax_policy": object()},
                    provenance={"source": "test"},
                    scope={"universe": "dataset", "symbols": (), "calendar_start": day.isoformat()},
                    calendar=(day, date(2020, 1, 2)), history=20)

        class Reader:
            def __init__(self, fields, history):
                self.fields, self.history = fields, history

            def query(self, _query):
                return Dataset(fields=self.fields, **{**base, "history": self.history})

        for fields, history in ((("other",), 20), (("close",), 19)):
            with self.subTest(fields=fields, history=history):
                with self.assertRaises(Exception):
                    DataBus(object(), Reader(fields, history)).query(query)

    def test_date_only_request_covers_the_entire_end_date(self):
        from protocal import parse_backtest_request

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
        from protocal import parse_backtest_request

        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limtis": {"lot_size": 10}})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"stock_weight": "not-a-number"}})
        with self.assertRaises(Exception):
            parse_backtest_request({**request_data(), "limits": {"adv_fraction": "NaN"}})

    def test_preflight_request_keeps_scope_and_benchmark_inputs(self):
        from protocal import parse_backtest_request

        request = parse_backtest_request({
            **{key: value for key, value in request_data().items() if key != "run_id"},
            "symbols": ["600036"],
            "fields": ["signal_close"], "history": 60,
            "benchmark_id": "hs300",
        })

        self.assertEqual(request.symbols, ("600036",))
        self.assertEqual(request.fields, ("signal_close",))
        self.assertEqual(request.history, 60)
        self.assertEqual(request.benchmark_id, "hs300")

    def test_config_plugin_params_merge_with_request_overrides(self):
        from core.calculation import AllocationLimits
        from protocal import _load_plugin, parse_backtest_request

        plugin = _load_plugin({"settlement": {
            "module": "tests.test_task3_protocol:ConfigSettlement",
            "params": {"mode": "configured", "limits": {"lot_size": 10}},
        }}, "settlement")
        request = parse_backtest_request({**request_data(), "limits": {"lot_size": 20}})
        value = plugin(100, object(), object(), **dict(request.settlement_parameters))
        self.assertEqual(value.mode, "configured")
        self.assertIsInstance(value.limits, AllocationLimits)
        self.assertEqual(value.limits.lot_size, 20)

        with self.assertRaises(ValueError):
            _load_plugin({"settlement": {
                "module": "tests.test_task3_protocol:ConfigSettlement",
                "params": {"misspelled": True},
            }}, "settlement")

    def test_backend_build_has_no_factory_parameter_and_factory_is_removed(self):
        import asyncio
        import json
        from protocal import build_backend, build_data
        import importlib.util
        ui = importlib.import_module("frontend.ui-protocol")

        self.assertEqual(list(inspect.signature(build_backend).parameters), ["root"])
        self.assertIsNone(importlib.util.find_spec("core.interfaces"))
        backend = build_backend(Path(__file__).resolve().parents[1])
        try:
            request = ui.UiRequest(
                "sources", "workbench.sources",
                "urn:lianghua:ui:input:workbench.sources:v1", {},
            )
            messages = asyncio.run(backend.send(request))
            self.assertEqual(messages[0].kind, "collection")
            self.assertIsInstance(messages[0].data, list)
            registry = json.loads(build_data(Path(__file__).resolve().parents[1]).read(
                "sources", "registry", "sources.json"))
            declared_ids = {item["id"] for item in registry["sources"]}
            returned_ids = [item["id"] for item in messages[0].data]
            self.assertEqual(len(returned_ids), len(set(returned_ids)))
            self.assertEqual(set(returned_ids), declared_ids)
            shape = {"id", "name", "frequency", "defaults", "job", "schedule",
                     "integrity_error", "snapshot", "records"}
            for item in messages[0].data:
                self.assertEqual(set(item), shape)
                self.assertIsInstance(item["defaults"], dict)
                self.assertIsInstance(item["records"], int)
        finally:
            backend.close()

    def test_kernel_port_uses_the_explicitly_injected_settlement_plugin(self):
        from protocal import KernelBacktestPort

        class Replacement:
            pass

        port = KernelBacktestPort(object(), Replacement)
        self.assertIs(port._kernel._settlement_type, Replacement)

    def test_kernel_query_settlement_matches_frozen_accounting(self):
        import json
        from dataclasses import asdict
        from decimal import Decimal
        from plugins.data import Settlement
        from protocal import _json_value
        from protocal import DataBus, _query
        from storage import DataStore
        from strategy import canonical_hash, load_strategy
        from protocal import BacktestRequest
        from core.kernel import Kernel
        from tests.history_fixture import make_snapshot

        frozen = json.loads(Path("tests/fixtures/kernel-history-ab.json").read_text())
        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            kernel = Kernel(bus, load_strategy, _query, Settlement)
            strategy = load_strategy("plugins.strategies.breadth_low_amount_reversal", {"positions": 4})
            request = BacktestRequest(
                "frozen", "plugins.strategies.breadth_low_amount_reversal", asdict(strategy.params), "fixture",
                datetime(2020, 1, 31, tzinfo=timezone.utc),
                datetime(2020, 2, 4, 23, 59, 59, tzinfo=timezone.utc), Decimal("100000"),
                settlement_parameters={
                    "limits": __import__("core.calculation", fromlist=["AllocationLimits"]).AllocationLimits(),
                    "execution": execution(),
                },
            )
            response = kernel.run(request)
            result = response.account_result
            self.assertEqual(response.final_equity, Decimal(str(frozen["equity"][-1]["equity"])))
            self.assertEqual([{"at": p.day.isoformat(), "equity": float(p.equity)}
                              for p in result.equity if p.day >= date(2020, 1, 31)], frozen["equity"])
            hashes = {key: canonical_hash(_json_value(getattr(result, key))) for key in frozen["trace_hashes"]}
            self.assertEqual(hashes, frozen["trace_hashes"])

    def test_protocol_saves_complete_kernel_trace_through_data_bus(self):
        import json
        from pathlib import Path
        from tests.history_fixture import make_snapshot
        from plugins.data import Settlement
        from protocal import DataBus, KernelBacktestPort
        from storage import DataStore

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, Settlement)
            value = port.handle("workbench.backtest", {
                "run_id": "saved", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "evaluation": {"periods": 12, "baseline": .01},
                **settlement_payload(),
            }).data
            manifest = json.loads(bus.read("runs", "saved", "manifest.json"))
            saved = json.loads(bus.read("runs", "saved", "result.json"))
            self.assertIn("trace", value)
            self.assertIn("metrics", value)
            self.assertIn("provenance", value)
            self.assertIsNone(value["provenance"]["as_of"])
            self.assertEqual(value["provenance"]["query_semantics"], "snapshot_replay")
            self.assertEqual(saved["trace"], value["trace"])
            self.assertEqual(saved["equity"], value["equity"])
            self.assertEqual(value["effective_config"]["evaluation"], {"periods": 12, "baseline": .01})
            self.assertEqual(manifest["effective_config"], value["effective_config"])
            self.assertEqual(manifest["config_hash"], value["config_hash"])

            changed = port.handle("workbench.backtest", {
                "run_id": "saved-lot", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "limits": {"lot_size": 300},
                "execution": execution(),
            }).data
            self.assertNotEqual(value["config_hash"], changed["config_hash"])
            self.assertEqual(changed["effective_config"]["settlement"]["params"]["limits"]["lot_size"], 300)

            changed_evaluation = port.handle("workbench.backtest", {
                "run_id": "saved-evaluation", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                "evaluation": {"periods": 252, "baseline": 0},
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
