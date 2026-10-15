import asyncio
import importlib
import json
import tempfile
import unittest
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from protocal import DataBus, DataError, Modules, Protocol
from storage import DataStore
from collect.methods import Methods


class CollectionTests(unittest.TestCase):
    def test_methods_merge_unique_current_records_without_batch_files(self):
        from collect.methods import Methods
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); methods = Methods(bus)
            source = {'id': 'custom', 'method': 'fixture', 'params': {'key_fields': ['identity']}}
            from collect.sources import Sources
            sources = Sources(bus)
            sources.register({**source, 'name': 'Custom', 'frequency': 'manual', 'defaults': {}})
            source = sources.get('custom')
            packet = {'records': [{'identity': 'a', 'value': 1}], 'raw': [], 'scope': {}}
            with self.assertRaises(DataError) as caught:
                methods.publish({**source, 'params': {}}, packet)
            self.assertEqual(caught.exception.code, 'identity_policy')
            methods.publish(source, packet)
            methods.publish(source, packet)
            view = methods.records(source, 0, 50)
            self.assertEqual(view['total'], 1)
            self.assertEqual(view['records'], packet['records'])
            before = bus.read('sources', 'custom', 'records.json')
            with self.assertRaises(DataError):
                methods.publish(source, {**packet, 'records': [{'identity': 'a', 'value': 2}]})
            self.assertEqual(bus.read('sources', 'custom', 'records.json'), before)
            self.assertFalse((Path(root) / 'data/source/snapshots').exists())
            self.assertFalse((Path(root) / 'log/custom/latest.json').exists())

    def test_collect_request_normalization_is_shared_and_rejects_before_fetch(self):
        from collect.methods import Methods

        source = {
            "id": "fixture", "name": "Fixture", "method": "fixture",
            "frequency": "daily", "data_type": "daily", "modes": ["auto", "fix"],
            "fields": ["date", "code", "open", "high", "low", "close", "volume",
                        "amount", "turn", "pctChg", "tradestatus", "isST", "peTTM",
                        "pbMRQ", "psTTM", "pcfNcfTTM"],
            "defaults": {"codes": ["sh.600036", "sz.000001"],
                         "fields": ["date", "code", "close"],
                         "provider_options": {"page_size": 100}},
        }
        methods = Methods(object())
        request = {"codes": " sh.600036, sz.000001, sh.000300, sh.600036 ",
                   "fields": "date, code, close, amount, close",
                   "start": "2026-10-01", "end": "2026-10-13",
                   "provider_options": {"page_size": 50}}
        auto = methods.prepare_request(source, {**request, "mode": "auto"})
        manual = methods.prepare_request(source, {**request, "mode": "fix"})
        for key in ("codes", "fields", "start", "end", "provider_options"):
            self.assertEqual(auto[key], manual[key])
        self.assertEqual(auto["codes"], ["sh.600036", "sz.000001", "sh.000300"])
        self.assertEqual(auto["fields"], ["close", "amount"])
        self.assertEqual(auto["provider_options"], {"page_size": 50})
        basic = methods.prepare_request(source, {"fields": "basic"})
        full = methods.prepare_request(source, {"fields": "full"})
        self.assertEqual(basic["fields"], ["open", "high", "low", "close", "volume",
                                            "amount", "turn", "pctChg", "tradestatus", "isST"])
        self.assertEqual(full["fields"], ["open", "high", "low", "close", "volume",
                                           "amount", "turn", "pctChg", "tradestatus", "isST",
                                           "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"])

        for invalid in (
            {"codes": " , ", "start": "2026-10-01", "end": "2026-10-13"},
            {"codes": ["sh.600036", ""], "start": "2026-10-01", "end": "2026-10-13"},
            {"fields": ",", "start": "2026-10-01", "end": "2026-10-13"},
            {"codes": "sh.600036", "start": "2026-10-14", "end": "2026-10-13"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                methods.prepare_request(source, invalid)

        with tempfile.TemporaryDirectory() as root:
            calls = []
            bus = DataBus(DataStore(Path(root)))
            from collect.core import Collect
            from collect.sources import Sources
            sources = Sources(bus)
            sources.register({**source, "defaults": {"codes": ["sh.600036"]}})
            methods = Methods(bus, {"fixture": lambda _source, _request: calls.append(True)})
            collect = Collect(sources, methods)
            with self.assertRaises(ValueError):
                collect.handle("lake.startFetch", {"source_id": "fixture", "codes": " , "})
            self.assertEqual(calls, [])
            self.assertIsNone(collect._fetch_thread)

    def test_daily_source_summary_and_page_do_not_build_a_backtest_dataset(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path).append_column(
                "float_mv", pa.array([None] * pq.read_table(path).num_rows, type=pa.float64()))
            pq.write_table(table, path)
            bus = DataBus(DataStore(root))
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily", "default": True,
                              "modes": ["query"], "dataset_id": "fixture",
                              "defaults": {}})
            collect = Collect(sources, Methods(bus))
            with patch.object(bus, "query", side_effect=AssertionError("no backtest query")):
                views = collect.handle("workbench.sources", {})
                page = collect.handle("workbench.snapshot", {
                    "id": "daily", "offset": 50, "limit": 10,
                    "start": "2020-01-02", "end": "2020-02-04",
                    "symbols": ["600000", "600001", "600002", "600003"],
                })
            self.assertGreater(page["total"], 50)
            self.assertEqual(len(page["records"]), 10)
            self.assertTrue(all(row["float_mv"] is None for row in page["records"]))
            self.assertTrue(views[0]["snapshot"]["snapshot_id"])
            self.assertGreater(views[0]["records"], page["total"])

    def test_full_fields_require_declared_provider_capability_and_packet_uses_request_metadata(self):
        from collect.methods import Methods

        source = {"id": "daily", "method": "fixture", "data_type": "daily",
                  "frequency": "manual", "fields": ["open", "close"],
                  "defaults": {}}
        methods = Methods(object(), {"fixture": lambda _source, _request: {
            "records": [], "raw": [], "scope": {"codes": []}}})
        with self.assertRaisesRegex(ValueError, "full"):
            methods.prepare_request(source, {"fields": "full"})
        packet = methods.fetch(source, methods.prepare_request(
            source, {"frequency": "d", "adjustment": "3", "fields": "basic"}))
        self.assertEqual(packet["frequency"], "d")
        self.assertEqual(packet["adjustment"], "3")
        self.assertEqual(packet["data_type"], "daily")

    def test_daily_source_summary_applies_registered_code_scope(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root)); sources = Sources(bus)
            sources.register({"id": "benchmark", "name": "Benchmark", "method": "fixture",
                              "frequency": "daily", "data_type": "daily", "modes": ["query"],
                              "defaults": {"codes": ["sh.600003"]}, "dataset_id": "fixture"})
            with patch.object(bus, "query", side_effect=AssertionError("no backtest query")):
                view = Collect(sources, Methods(bus)).handle("workbench.sources", {})[0]
            self.assertEqual(view["records"], 157)
            self.assertEqual(view["snapshot"]["scope"]["symbols"], ["sh.600003"])

    def test_daily_page_matches_only_normalized_security_aliases(self):
        import hashlib
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.sources import Sources
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            row = {"date": "2020-01-02", "open": 10.0, "high": 11.0,
                   "low": 9.0, "close": 10.5, "volume": 100.0,
                   "amount": 1050.0, "tradestatus": 1.0}
            pq.write_table(pa.Table.from_pylist([
                {**row, "code": "000001"},
                {**row, "code": "sz.000001"},
                {**row, "code": "sh.000001"},
            ]), path)
            manifest_path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["scope"]["symbols"] = ["000001", "sh.000001", "sz.000001"]
            daily_ref = next(item for item in manifest["files"]
                             if item["file"] == "daily/bars.parquet")
            daily_ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))

            bus = DataBus(DataStore(root))
            sources = Sources(bus)
            for source_id, code in (("sh_index", "sh.000001"),
                                    ("bank", "000001")):
                sources.register({"id": source_id, "name": source_id,
                                  "method": "fixture", "frequency": "daily",
                                  "data_type": "daily", "modes": ["query"],
                                  "dataset_id": "fixture",
                                  "defaults": {"codes": [code]}})

            index = Methods(bus).daily_collection_summary(sources.get('sh_index'), "fixture")
            index_page = Methods(bus).records(sources.get('sh_index'),
                                                   limit=10)
            bank = Methods(bus).daily_collection_summary(sources.get('bank'), "fixture")
            bank_page = Methods(bus).records(sources.get('bank'), limit=10)
            self.assertEqual(index["records_count"], 1)
            self.assertEqual(index_page["total"], 1)
            self.assertEqual([row["code"] for row in index_page["records"]],
                             ["sh.000001"])
            self.assertEqual(bank["records_count"], 2)
            self.assertEqual(bank_page["total"], 2)
            self.assertEqual({row["code"] for row in bank_page["records"]},
                             {"000001", "sz.000001"})

    def test_daily_source_calendar_is_loaded_and_fix_enters_lake(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            calendar = {"sessions": ["2026-01-02", "2026-01-05", "2026-01-06"]}
            calendar_ref = bus.write("sources", "lake", "calendar.json",
                                     json.dumps(calendar).encode())
            bus.write("sources", "lake", "manifest.json", json.dumps({
                "schema": 2,
                "files": [{"file": "calendar.json", "sha256": calendar_ref.sha256,
                           "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600036"], "universe": "market"},
                "calendar": "calendar.json",
            }).encode())
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily",
                              "modes": ["fix"], "default": True,
                              "storage_id": "lake", "dataset_id": "lake",
                              "calendar": "calendar.json", "price_basis": "raw",
                              "defaults": {}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"code": "600036", "date": "2026-01-05",
                              "open": 10.0, "high": 11.0, "low": 9.0,
                              "close": 10.5, "volume": 100.0, "amount": 1050.0}],
                "raw": [], "scope": {"universe": "market"}}})
            collect = Collect(sources, methods)
            fetched = collect.handle("lake.startFetch", {
                "mode": "fix", "codes": ["sh.600036"],
                "start": "2026-01-05", "end": "2026-01-05",
            })
            self.assertTrue(fetched["ok"])
            collect._fetch_thread.join(2)
            self.assertEqual(collect.handle("lake.fetchJob", {})["status"], "fetched")
            self.assertTrue(collect.handle("lake.startMerge", {"dryRun": False})["ok"])
            collect._merge_thread.join(2)
            parquet = pq.read_table(pa.BufferReader(
                bus.read("sources", "lake", "daily/2026.parquet")))
            self.assertEqual(parquet.to_pylist()[0]["code"], "600036")

    def test_string_daily_provider_rows_close_the_standard_publish_query_loop(self):
        import sys
        from datetime import date
        from protocal import DataQuery
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            calendar = {"sessions": ["2026-01-02", "2026-01-05", "2026-01-06"]}
            calendar_ref = bus.write("sources", "lake", "calendar.json",
                                     json.dumps(calendar).encode())
            bus.write("sources", "lake", "manifest.json", json.dumps({
                "schema": 2,
                "files": [{"file": "calendar.json", "sha256": calendar_ref.sha256,
                           "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600036"], "universe": "market"},
                "calendar": "calendar.json",
            }).encode())
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "daily",
                              "frequency": "manual", "data_type": "daily",
                              "modes": ["fix"], "default": True,
                              "storage_id": "lake", "dataset_id": "lake",
                              "calendar": "calendar.json", "price_basis": "raw",
                              "defaults": {}})
            fields = ["date", "code", "open", "high", "low", "close", "volume",
                      "amount", "turn", "pctChg", "tradestatus", "isST"]
            trade_dates = ["calendar_date", "is_trading_day"]
            class Rows:
                error_code = "0"
                error_msg = ""
                def __init__(self):
                    self.rows = [["2026-01-07", "sh.600036", "10.0", "11.0", "9.0",
                                  "10.5", "100", "1050", "1.2", "0.5", "1", "0"]]
                def next(self):
                    return bool(self.rows)
                def get_row_data(self):
                    return self.rows.pop(0)
            Rows.fields = fields
            class TradeDates:
                error_code = "0"
                error_msg = ""
                fields = trade_dates
                def __init__(self):
                    self.rows = [["2026-01-07", "1"], ["2026-01-08", "1"]]
                def next(self):
                    return bool(self.rows)
                def get_row_data(self):
                    return self.rows.pop(0)
            provider = SimpleNamespace(
                login=lambda: SimpleNamespace(error_code="0", error_msg=""),
                logout=lambda: None,
                query_trade_dates=lambda *args, **kwargs: TradeDates(),
                query_history_k_data_plus=lambda *args, **kwargs: Rows())
            methods = Methods(bus)
            collect = Collect(sources, methods)
            with patch.dict(sys.modules, {"baostock": provider}):
                fetched = collect.handle("lake.startFetch", {
                    "source_id": "daily", "codes": "600036", "fields": "basic",
                    "start": "2026-01-07", "end": "2026-01-07",
                    "calendar_end": "2026-01-08"})
                self.assertTrue(fetched["ok"])
                collect._fetch_thread.join(2)
                self.assertEqual(collect.handle("lake.fetchJob", {})["status"], "fetched")
                packet = collect._packets[collect._current_job_id]
                history = next(item for item in packet["raw"]
                               if item["response"]["fields"][0] == "date")
                self.assertEqual(history["response"]["rows"][0][2], "10.0")
                self.assertEqual(packet["calendar"], ["2026-01-02", "2026-01-05",
                                                       "2026-01-06", "2026-01-07",
                                                       "2026-01-08"])
                self.assertEqual(packet["records"][0]["open"], 10.0)
                self.assertIsInstance(packet["records"][0]["close"], float)
                self.assertTrue(collect.handle("lake.startMerge", {"dryRun": True})["ok"])
                collect._merge_thread.join(2)
                self.assertIsNone(collect.handle("lake.mergeJob", {})["error"])
                self.assertTrue(collect.handle("lake.startMerge", {"dryRun": False})["ok"])
                collect._merge_thread.join(2)
                self.assertIsNone(collect.handle("lake.mergeJob", {})["error"])
            dataset = bus.query(DataQuery(
                "lake", date(2026, 1, 7), date(2026, 1, 7),
                ("open", "high", "low", "close", "volume", "amount"), 1, "raw",
                symbols=("sh.600036",), universe="market"))
            values = dataset.records[0].frame.fields["sh.600036"]
            self.assertEqual(values["close"], 10.5)

    def test_data_bus_rejects_noncanonical_daily_packets_before_any_write(self):
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            row = {"code": "600036", "date": "2026-01-02", "open": 10.0,
                   "high": 11.0, "low": 9.0, "close": 10.5,
                   "volume": 1.0, "amount": 2.0}
            for request in (
                {"frequency": "m", "price_basis": "raw"},
                {"frequency": "daily", "price_basis": "adjusted"},
                {"frequency": "daily", "adjustment": "2"},
                {"frequency": "daily", "fields": ["status"]},
            ):
                with self.subTest(request=request), self.assertRaises(DataError) as caught:
                    Methods(bus).publish({'id': 'fixture', 'data_type': 'daily',
                                          'storage_id': 'lake', 'dataset_id': 'fixture'}, {
                        "records": [row], "calendar": ["2026-01-02"],
                        "raw": [],
                        "request": request, "scope": {"universe": "market"},
                    })
                self.assertEqual(caught.exception.code, "canonical_daily")
            self.assertEqual(bus._store.publish_states(), ())
            with self.assertRaises(DataError):
                bus.read("sources", "lake", "daily/2026.parquet")

    def test_lake_query_keeps_frontend_security_search_shape(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            lake = root / "data/source/lake"
            lake.mkdir(parents=True)
            pq.write_table(pa.Table.from_pylist([{
                "code": "600036", "name": "招商银行", "exchange": "SH",
                "status": "listed", "first_date": "2002-04-09", "last_date": None,
            }]), lake / "stock_info.parquet")
            pq.write_table(pa.Table.from_pylist([{
                "code": "600036", "date": "2026-09-11", "close": 42.5,
                "pctChg": 1.25, "amount": 123456789.0,
            }]), lake / "latest_snapshot.parquet")
            bus = DataBus(DataStore(root))
            sources = Sources(bus)
            result = Collect(sources, Methods(bus)).handle("lake.query", {"q": "招商"})
            self.assertEqual(result["items"][0]["代码"], "600036")
            self.assertEqual(result["items"][0]["名称"], "招商银行")
            self.assertEqual(result["items"][0]["最新收盘"], 42.5)
            self.assertEqual(result["items"][0]["涨跌幅"], 1.25)
            self.assertEqual(result["items"][0]["行情日期"], "2026-09-11")
            self.assertAlmostEqual(result["items"][0]["成交额亿"], 1.23456789)

    def test_lake_kline_keeps_frontend_curve_shape(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        from protocal import Bar, Dataset, HistoryDay, MarketFrame, HistoryBar
        from datetime import date, datetime, timezone

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily",
                              "modes": ["kline"], "default": True,
                              "dataset_id": "market", "calendar": "calendar.json",
                              "defaults": {}})
            bus.write("sources", "market", "calendar.json",
                      json.dumps({"sessions": ["2026-01-02", "2026-01-03"]}).encode())
            collect = Collect(sources, Methods(bus))
            at = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
            frame = MarketFrame(at, "MARKET",
                                {"600036": {"open": 10.0, "high": 11.0,
                                             "low": 9.0, "close": 10.5,
                                             "volume": 100.0, "amount": 1050.0}},
                                {}, {"600036": (HistoryBar(at,
                                                             {"close": 10.5}),)})
            dataset = Dataset("market", (HistoryDay(
                trade_date=date(2026, 1, 2), frame=frame,
                bars={"600036": Bar("600036", date(2026, 1, 2),
                                    10.0, 11.0, 9.0, 10.5, 100.0, 1050.0)},
                previous_closes={}, opening_marks={}, closing_marks={},
                opening_amount20={}, closing_amount20={}, liquidity_as_of=date.min,
                registrations=(), opening_volume20={}),),
                               {"kind": "raw"}, {}, {"universe": "market", "symbols": ("600036",)},
                               (date(2026, 1, 2), date(2026, 1, 3)),
                               ("open", "high", "low", "close", "volume", "amount"), 1)
            bus.query = lambda query: dataset
            result = collect.handle("lake.kline", {"code": "600036", "freq": "日",
                                                     "adj": "不复权"})
            self.assertEqual(result["dates"], ["2026-01-02"])
            self.assertEqual(result["candles"], [[10.0, 10.5, 9.0, 11.0]])

    def test_lake_fetch_and_merge_share_current_task_and_dry_run_does_not_publish(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "daily", "name": "Daily", "method": "fixture",
                              "frequency": "daily", "data_type": "daily",
                              "modes": ["auto", "fix"], "default": True,
                              "defaults": {}})
            published = []
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"code": "600036", "date": "2026-01-02",
                              "open": 10.0, "high": 11.0, "low": 9.0,
                              "close": 10.5, "volume": 1.0, "amount": 2.0}],
                "raw": [], "scope": {"universe": "market"}}})
            methods.publish = lambda source, packet: published.append(packet)
            methods.prepare = lambda source, packet: {"records": len(packet["records"]), "incoming": len(packet["records"])}
            collect = Collect(sources, methods)
            fetched = collect.handle("lake.startFetch", {
                "mode": "fix", "start": "2026-01-02", "end": "2026-01-02",
                "codes": "600036", "fields": "basic", "force": False,
                "sleep": 0.0, "max_tasks": 0,
            })
            self.assertTrue(fetched["ok"])
            self.assertEqual(published, [])
            collect._fetch_thread.join(2)
            self.assertEqual(collect.handle("lake.fetchJob", {} )["status"], "fetched")
            self.assertTrue(collect.handle("lake.startMerge", {"dryRun": True})["ok"])
            collect._merge_thread.join(2)
            self.assertEqual(published, [])
            self.assertTrue(collect.handle("lake.startMerge", {"dryRun": False})["ok"])
            collect._merge_thread.join(2)
            self.assertEqual(len(published), 1)

    def test_sources_concurrent_instances_merge_registry_updates(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            left, right = Sources(bus), Sources(bus)
            def descriptor(identity):
                return {"id": identity, "name": identity, "method": identity,
                        "frequency": "daily", "defaults": {}}
            errors = []
            threads = [
                threading.Thread(target=lambda: left.register(descriptor("left"))),
                threading.Thread(target=lambda: right.register(descriptor("right"))),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual({item["id"] for item in Sources(bus).list()},
                             {"left", "right"})

    def test_sources_protocol_returns_frontend_source_array_with_runtime_fields(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            collect = Collect(sources, Methods(bus, {}))
            result = collect.handle("workbench.sources", {})
            self.assertIsInstance(result, list)
            self.assertEqual(set(result[0]), {
                "id", "name", "frequency", "defaults", "job", "schedule",
                "integrity_error", "snapshot", "records",
            })

    def test_source_changes_preserve_datasets_and_benchmarks(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            registry = {"sources": [], "datasets": [{"id": "history_holdout"}],
                        "benchmarks": [{"id": "hs300"}]}
            bus.write("sources", "registry", "sources.json",
                      json.dumps(registry).encode())
            sources = Sources(bus)
            descriptor = {"id": "fixture", "name": "Fixture", "method": "fixture",
                          "frequency": "daily", "defaults": {"codes": ["600001"]}}
            sources.register(descriptor)
            sources.update("fixture", {"lookback_days": 3})
            sources.remove("fixture")
            saved = json.loads(bus.read("sources", "registry", "sources.json"))
            self.assertEqual(saved["datasets"], registry["datasets"])
            self.assertEqual(saved["benchmarks"], registry["benchmarks"])
            self.assertEqual(saved["sources"], [])

    def test_sources_registers_dynamic_descriptors_through_databus(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            descriptor = {
                'id': 'fixture', 'name': 'Fixture', 'method': 'fixture',
                'frequency': 'daily', 'defaults': {'codes': ['600001']},
            }
            self.assertEqual(sources.register(descriptor), descriptor)
            with self.assertRaises(ValueError):
                sources.register(descriptor)
            restarted = Sources(DataBus(DataStore(Path(root))))
            self.assertEqual(restarted.get('fixture'), descriptor)

    def test_sources_accepts_registered_market_descriptor_fields(self):
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            sources = Sources(DataBus(DataStore(Path(root))))
            descriptor = {
                "id": "market_daily", "name": "Market daily", "method": "daily",
                "frequency": "daily", "data_type": "daily", "provider": "fixture",
                "fields": ["open", "high", "low", "close", "volume", "amount"],
                "storage_id": "lake", "dataset_id": "market", "calendar": "market_calendar",
                "defaults": {"codes": ["sh.600000"]},
            }
            self.assertEqual(sources.register(descriptor), descriptor)

    def test_methods_instantiates_same_method_per_source_configuration(self):
        from collect.methods import Methods

        class Probe:
            def __init__(self, source):
                self.source = source
            def collect(self, request):
                return {"records": [{"value": self.source["defaults"]["value"]}],
                        "raw": [], "scope": {"source": self.source["id"]}}

        with tempfile.TemporaryDirectory() as root:
            methods = Methods(DataBus(DataStore(Path(root))), {"probe": Probe})
            left = {"id": "left", "method": "probe", "defaults": {"value": 1}}
            right = {"id": "right", "method": "probe", "defaults": {"value": 2}}
            self.assertEqual(methods.fetch(left, {})["records"][0]["value"], 1)
            self.assertEqual(methods.fetch(right, {})["records"][0]["value"], 2)

    def test_backend_starts_with_empty_registry_and_new_source_needs_no_rebuild(self):
        from protocal import build_backend

        with tempfile.TemporaryDirectory() as root:
            backend = build_backend(Path(root))
            try:
                self.assertEqual(backend._modules.sources.sources.list(), [])
                backend._modules.sources.sources.register({
                    "id": "new_quotes", "name": "New quotes", "method": "quotes",
                    "frequency": "daily", "defaults": {"codes": ["1.600000"]},
                })
                with patch("collect.methods.EastmoneyQuotes.collect", return_value={
                        "records": [{"code": "1.600000", "data_at": "2026-10-14T15:00:00+08:00",
                                     "source": "new_quotes"}], "raw": [],
                        "scope": {"kind": "fixture"}}):
                    result = backend._modules.sources.run({"source_id": "new_quotes"})
                self.assertEqual(result["status"], "succeeded")
            finally:
                backend.close()

    def test_methods_fetch_publish_and_log_use_databus_and_release_as_one_batch(self):
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {
                'fixture': lambda source, request: {
                    'records': [{'code': '600001', 'date': request['date'], 'close': 10.0, 'raw_page': 0}],
                    'raw': [{'source': source['id'], 'request': request}],
                    'data_as_of': request['date'],
                    'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
                },
            })
            source = {'id': 'fixture', 'method': 'fixture', 'params': {'key_fields': ['code', 'date']}}
            packet = methods.fetch(source, {'date': '2026-10-14'})
            published = methods.publish(source, packet)
            logged = methods.log({'source_id': 'fixture', 'event': 'published'})
            self.assertEqual(published.kind, 'sources')
            self.assertEqual(logged.kind, 'state')
            view = methods.records(source)
            self.assertEqual(view['records'][0]['close'], 10.0)
            self.assertEqual(view['raw'], packet['raw'])
            self.assertEqual(bus.list('sources', 'snapshots'), ())
            self.assertEqual(json.loads(bus.read('state', 'collect', logged.name))['event'], 'published')

    def test_methods_publish_accepts_non_security_packets_and_source_id_is_authoritative(self):
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            methods = Methods(bus, {
                'news': lambda source, request: {
                    'source_id': 'spoofed',
                    'records': [{'title': '公告'}], 'raw': [{'url': 'x'}],
                    'scope': {'category': 'announcement'},
                },
            })
            source = {'id': 'announcements', 'method': 'fixture', 'params': {'key_fields': ['title']}}
            packet = methods.fetch({**source, 'method': 'news'},
                                  {'date': '2026-10-14'})
            self.assertEqual(packet['source_id'], 'announcements')
            published = methods.publish(source, packet)
            self.assertEqual(published.kind, 'sources')
            self.assertTrue(methods.records(source)['snapshot_id'])

    def test_collect_manual_run_uses_registered_source_and_one_methods_path(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({'id': 'fixture', 'name': 'Fixture', 'method': 'fixture',
                              'frequency': 'daily', 'defaults': {},
                              'params': {'key_fields': ['code', 'date']}})
            methods = Methods(bus, {
                'fixture': lambda source, request: {
                    'records': [{'code': '600001', 'date': request['date'], 'close': 10.0}],
                    'raw': [], 'data_as_of': request['date'],
                    'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
                },
            })
            collect = Collect(sources, methods)
            result = collect.run({'source_id': 'fixture', 'date': '2026-10-14'})
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(result['records'], 1)
            self.assertEqual(collect.status(result['job_id'])['status'], 'succeeded')
            collect.close()

    def test_collect_freezes_source_defaults_and_request_overrides_them(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {"codes": ["old"]}})
            seen = []
            methods = Methods(bus, {
                "fixture": lambda source, request: (
                    seen.append((source["defaults"]["codes"][:], request["codes"][:])) or
                    {"records": [], "raw": [], "scope": {"category": "generic"}}),
            })
            collect = Collect(sources, methods)
            collect.run({"source_id": "fixture", "codes": ["new"]})
            self.assertEqual(seen, [(["old"], ["new"])])

    def test_default_http_transport_decodes_json_for_quote_and_announcement_methods(self):
        from collect.methods import CninfoAnnouncements, EastmoneyQuotes

        class Response:
            def __init__(self, value):
                self.value = json.dumps(value).encode()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.value

        quote = {"data": {"diff": [{
            "f2": 10.0, "f3": 1.0, "f5": 2.0, "f6": 20.0,
            "f12": "600000", "f13": 1, "f14": "Fixture", "f124": 1700000000,
        }]}}
        announcement = {"totalRecordNum": 0, "hasMore": False, "announcements": []}
        with patch("collect.methods.urlopen",
                   side_effect=[Response(quote), Response(announcement)]):
            result = EastmoneyQuotes({}).collect({"codes": ["1.600000"]})
            self.assertEqual(result["records"][0]["code"], "1.600000")
            result = CninfoAnnouncements({}).collect({
                "start": "2026-01-01", "end": "2026-01-01"})
            self.assertEqual(result["records"], [])

    def test_daily_publish_merges_existing_year_parquet_and_preserves_columns(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            existing = pa.Table.from_pylist([
                {"code": "600000", "date": "2025-01-02", "name": "A",
                 "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                 "volume": 100.0, "amount": 1050.0},
            ])
            output = pa.BufferOutputStream()
            pq.write_table(existing, output)
            daily_ref = bus.write("sources", "lake", "daily/2025.parquet",
                                  output.getvalue().to_pybytes())
            calendar = {"sessions": ["2025-01-02"], "source_file": "calendar_source.json"}
            calendar_ref = bus.write("sources", "fixture", "calendar.json",
                                     json.dumps(calendar).encode())
            manifest = {"schema": 2,
                        "files": [
                            {"file": "calendar.json", "sha256": calendar_ref.sha256, "role": "market"},
                            {"file": "source/lake/daily/2025.parquet",
                             "sha256": daily_ref.sha256, "role": "market"},
                        ],
                        "fields": {}, "scope": {"symbols": ["600000"], "universe": "market"},
                        "calendar": "calendar.json"}
            bus.write("sources", "fixture", "manifest.json", json.dumps(manifest).encode())
            source = {"id": "daily", "method": "daily", "data_type": "daily",
                      "storage_id": "lake", "dataset_id": "fixture",
                      "calendar": ["2025-01-02", "2025-01-03"],
                      "name": "Daily", "frequency": "daily", "defaults": {}}
            methods = Methods(bus, {"daily": lambda _source, request: {
                "records": request["records"], "raw": [], "scope": {"universe": "market"}}})
            methods.publish(source, methods.fetch(source, {"records": [
                {"code": "600000", "date": "2025-01-02", "name": "A",
                 "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                 "volume": 100.0, "amount": 1050.0},
                {"code": "600000", "date": "2025-01-03", "name": "A",
                 "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.5,
                 "volume": 120.0, "amount": 1380.0},
            ]}))
            table = pq.read_table(bus.path("sources", "lake", "daily/2025.parquet"))
            self.assertEqual(table.schema.names, existing.schema.names)
            self.assertEqual(len(table), 2)
            self.assertEqual(sorted(row["date"] for row in table.to_pylist()),
                             ["2025-01-02", "2025-01-03"])
            saved_calendar = json.loads(bus.read("sources", "fixture", "calendar.json"))
            self.assertEqual(saved_calendar["sessions"], ["2025-01-02", "2025-01-03"])
            saved_manifest = json.loads(bus.read("sources", "fixture", "manifest.json"))
            self.assertEqual(saved_manifest["scope"], {
                "symbols": ["600000"], "universe": "market",
                "calendar_start": "2025-01-02", "calendar_end": "2025-01-03",
            })
            self.assertEqual(saved_manifest["fields"], {})

    def test_daily_conflict_is_rejected_before_any_parquet_write(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            table = pa.Table.from_pylist([{"code": "600000", "date": "2025-01-02",
                                           "open": 10.0, "high": 11.0, "low": 9.0,
                                           "close": 10.0, "volume": 1.0, "amount": 10.0}])
            output = pa.BufferOutputStream()
            pq.write_table(table, output)
            before = bus.write("sources", "lake", "daily/2025.parquet",
                               output.getvalue().to_pybytes())
            calendar_ref = bus.write("sources", "fixture", "calendar.json",
                                     b'{"sessions":["2025-01-02"]}')
            manifest = {"schema": 2, "files": [
                {"file": "calendar.json", "sha256": calendar_ref.sha256, "role": "market"},
                {"file": "source/lake/daily/2025.parquet",
                 "sha256": before.sha256, "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600000"], "universe": "market"},
                "calendar": "calendar.json"}
            bus.write("sources", "fixture", "manifest.json", json.dumps(manifest).encode())
            methods = Methods(bus, {"daily": lambda _source, request: {
                "records": request["records"], "raw": [], "scope": {"universe": "market"}}})
            source = {"id": "daily", "method": "daily", "data_type": "daily",
                      "storage_id": "lake", "dataset_id": "fixture",
                      "calendar": ["2025-01-02"]}
            with self.assertRaises(DataError) as error:
                methods.publish(source, methods.fetch(source, {"records": [{
                    "code": "600000", "date": "2025-01-02", "open": 10.0,
                    "high": 11.0, "low": 9.0, "close": 99.0,
                    "volume": 1.0, "amount": 10.0}]}))
            self.assertEqual(error.exception.code, "data_conflict")
            self.assertEqual(bus.read_ref(before), bus.read("sources", "lake", "daily/2025.parquet"))
            self.assertEqual(bus._store.publish_states(), ())

    def test_daily_schema_error_writes_nothing_and_leaves_no_publication_marker(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.methods import Methods

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            table = pa.Table.from_pylist([{
                "code": "600000", "date": "2025-01-02", "name": "A",
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
                "volume": 1.0, "amount": 10.0,
            }])
            output = pa.BufferOutputStream()
            pq.write_table(table, output)
            before_daily = bus.write("sources", "lake", "daily/2025.parquet",
                                     output.getvalue().to_pybytes())
            before_calendar = bus.write(
                "sources", "fixture", "calendar.json",
                b'{"sessions":["2025-01-02"],"source_file":"calendar.json"}',
            )
            manifest = {"schema": 2, "files": [
                {"file": "calendar.json", "sha256": before_calendar.sha256, "role": "market"},
                {"file": "source/lake/daily/2025.parquet",
                 "sha256": before_daily.sha256, "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600000"], "universe": "market"},
                "calendar": "calendar.json"}
            before_manifest = bus.write(
                "sources", "fixture", "manifest.json", json.dumps(manifest).encode())
            methods = Methods(bus, {"daily": lambda _source, request: {
                "records": request["records"], "raw": [], "scope": {"universe": "market"}}})
            source = {"id": "daily", "method": "daily", "data_type": "daily",
                      "storage_id": "lake", "dataset_id": "fixture",
                      "calendar": ["2025-01-02", "2025-01-03"]}
            with self.assertRaises((TypeError, ValueError)):
                methods.publish(source, methods.fetch(source, {"records": [{
                    "code": "600000", "date": "2025-01-03", "name": {"bad": "schema"},
                    "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.0,
                    "volume": 2.0, "amount": 22.0,
                }]}))
            self.assertEqual(bus.read_ref(before_daily),
                             bus.read("sources", "lake", "daily/2025.parquet"))
            self.assertEqual(bus.read_ref(before_calendar),
                             bus.read("sources", "fixture", "calendar.json"))
            self.assertEqual(bus.read_ref(before_manifest),
                             bus.read("sources", "fixture", "manifest.json"))
            self.assertEqual(bus._store.publish_states(), ())

    def test_daily_publish_is_queryable_raw_across_years_and_idempotent(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from collect.methods import Methods
        from protocal import DataQuery
        from datetime import date

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            def write_year(year, rows):
                output = pa.BufferOutputStream()
                pq.write_table(pa.Table.from_pylist(rows), output)
                return bus.write("sources", "lake", f"daily/{year}.parquet",
                                 output.getvalue().to_pybytes())
            first = write_year(2024, [{
                "code": "600000", "date": "2024-12-31", "name": "A",
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "volume": 100.0, "amount": 1050.0,
            }])
            second = write_year(2025, [{
                "code": "600000", "date": "2025-01-02", "name": "A",
                "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.5,
                "volume": 120.0, "amount": 1380.0,
            }])
            calendar = {"sessions": ["2024-12-31", "2025-01-02", "2025-01-03", "2025-01-04"]}
            calendar_ref = bus.write("sources", "fixture", "calendar.json",
                                     json.dumps(calendar).encode())
            manifest = {"schema": 2, "files": [
                {"file": "calendar.json", "sha256": calendar_ref.sha256, "role": "market"},
                {"file": "source/lake/daily/2024.parquet", "sha256": first.sha256, "role": "market"},
                {"file": "source/lake/daily/2025.parquet", "sha256": second.sha256, "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600000"], "universe": "market"},
                "calendar": "calendar.json"}
            bus.write("sources", "fixture", "manifest.json", json.dumps(manifest).encode())
            methods = Methods(bus, {"daily": lambda _source, request: {
                "records": request["records"], "raw": [], "scope": {"universe": "market"}}})
            source = {"id": "daily", "method": "daily", "data_type": "daily",
                      "storage_id": "lake", "dataset_id": "fixture", "calendar": calendar["sessions"]}
            packet = {"records": [{
                "code": "600000", "date": "2025-01-03", "name": "A",
                "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5,
                "volume": 140.0, "amount": 1750.0,
            }]}
            methods.publish(source, methods.fetch(source, packet))
            methods.publish(source, methods.fetch(source, packet))
            dataset = bus.query(DataQuery(
                "fixture", date(2024, 12, 31), date(2025, 1, 3),
                ("open", "high", "low", "close", "volume", "amount"),
                1, "raw", symbols=("600000",), universe="market"))
            self.assertEqual([record.trade_date for record in dataset.records], [
                date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3)])
            self.assertEqual(dataset.records[-1].bars["600000"].close, 12.5)
            self.assertEqual(len(dataset.records[-1].frame.history["600000"]), 1)

    def test_daily_publish_exposes_workbench_view_without_snapshot_copy(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            output = pa.BufferOutputStream()
            pq.write_table(pa.Table.from_pylist([{
                "code": "600000", "date": "2026-01-02", "open": 10.0,
                "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0,
                "amount": 1050.0, "close_hfq": 10.5,
            }]), output)
            old = bus.write("sources", "lake", "daily/2026.parquet", output.getvalue().to_pybytes())
            for name, rows in (
                ("stock_info.parquet", [{"code": "600000", "status": "listed"}]),
                ("valuation_factors.parquet", [{"code": "600000", "quarter": "2026Q1"}]),
                ("fundamentals.parquet", [{"code": "600000", "quarter": "2026Q1"}]),
            ):
                table_output = pa.BufferOutputStream()
                pq.write_table(pa.Table.from_pylist(rows), table_output)
                bus.write("sources", "lake", name, table_output.getvalue().to_pybytes())
            bus.write("sources", "research", "security_code_changes.json",
                      json.dumps({"events": []}).encode())
            calendar = bus.write("sources", "market", "calendar.json",
                                 json.dumps({"sessions": ["2026-01-02", "2026-01-05", "2026-01-06"]}).encode())
            manifest = {"schema": 2, "files": [
                {"file": "calendar.json", "sha256": calendar.sha256, "role": "market"},
                {"file": "source/lake/daily/2026.parquet", "sha256": old.sha256, "role": "market"}],
                "fields": {}, "scope": {"symbols": ["600000"], "universe": "market"},
                "calendar": "calendar.json"}
            bus.write("sources", "market", "manifest.json", json.dumps(manifest).encode())
            sources = Sources(bus)
            sources.register({"id": "market", "name": "Market", "method": "fixture",
                              "frequency": "daily", "data_type": "daily", "storage_id": "lake",
                              "dataset_id": "market", "calendar": "calendar.json", "defaults": {}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"code": "600000", "date": "2026-01-05", "open": 11.0,
                              "high": 12.0, "low": 10.0, "close": 11.5, "volume": 120.0,
                              "amount": 1380.0, "close_hfq": 11.5}], "calendar": ["2026-01-02", "2026-01-05", "2026-01-06"],
                "raw": [], "scope": {"symbols": ["600000"]}}})
            job = Collect(sources, methods).run({"source_id": "market", "force": True})
            self.assertEqual(job["status"], "succeeded")
            self.assertTrue(job["snapshot_id"])
            self.assertGreater(job["records"], 0)
            collect = Collect(sources, methods)
            source_view = collect.handle("workbench.sources", {})[0]
            self.assertGreater(source_view["records"], 0)
            page = collect.handle("workbench.snapshot", {"id": "market", "offset": 0, "limit": 1})
            self.assertEqual(job["snapshot_id"], page["snapshot_id"])
            self.assertEqual(page["total"], 2)
            evidence = collect.handle("workbench.evidence", {"id": "market", "index": 0})
            self.assertEqual(evidence["snapshot_id"], page["snapshot_id"])
            self.assertIn("close", evidence["record"])
            self.assertEqual(bus.list("sources", "snapshots"), ())
            profile = collect.handle("lake.status", {"rescan": True})
            self.assertEqual(profile["总览"]["日线行情"]["股票数"], 1)
            inputs = collect.handle("lake.researchInputs", {})
            self.assertEqual(inputs["summary"]["symbols"], 1)
            self.assertEqual(collect.handle("lake.doctor", {})["items"], [])

    def test_lake_reports_use_actual_databus_facts(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))

            def write_table(name, rows):
                output = pa.BufferOutputStream()
                pq.write_table(pa.Table.from_pylist(rows), output)
                bus.write("sources", "lake", name, output.getvalue().to_pybytes())

            write_table("daily/2025.parquet", [
                {"code": "600036", "date": "2025-01-02", "open": 10.0,
                 "high": 11.0, "low": 9.0, "close": 10.5, "volume": 10.0,
                 "amount": 100.0, "tradestatus": 1},
                {"code": "600036", "date": "2025-01-02", "open": 10.0,
                 "high": 11.0, "low": 9.0, "close": 10.5, "volume": 10.0,
                 "amount": 100.0, "tradestatus": 1},
                {"code": "600036", "date": "2025-01-03", "open": None,
                 "high": 8.0, "low": 9.0, "close": -1.0, "volume": 5.0,
                 "amount": 50.0, "tradestatus": 0},
                {"code": "sh.000001", "date": "2025-01-02", "open": 20.0,
                 "high": 21.0, "low": 19.0, "close": 20.5, "volume": 0.0,
                 "amount": 0.0, "tradestatus": 1},
            ])
            write_table("stock_info.parquet", [
                {"code": "600036", "status": "listed"},
                {"code": "sh.000001", "status": "delisted"},
            ])
            write_table("valuation_factors.parquet", [
                {"code": "600036", "quarter": "2025Q1", "pe_ttm": 8.0},
            ])
            write_table("fundamentals.parquet", [
                {"code": "600036", "quarter": "2025Q1", "roe": None},
            ])
            bus.write("sources", "research", "security_code_changes.json", json.dumps({
                "events": [{"old_code": "600036", "new_code": "600037",
                            "effective_date": "2025-01-03",
                            "factor_basis_continuity_verified": False}]
            }).encode())
            bus.write("sources", "research_cache_verified", "adjust_600036.json", json.dumps({
                "schema": 2, "status": "ok", "raw_response": {"rows": []}
            }).encode())

            facts = bus.lake_facts()
            self.assertEqual(facts["daily"]["rows"], 4)
            self.assertEqual(facts["daily"]["symbols"], 2)
            self.assertEqual(facts["daily"]["duplicate_keys"], 1)
            self.assertEqual(facts["daily"]["invalid_price_rows"], 1)
            self.assertEqual(facts["daily"]["missing_fields"]["open"], 1)
            self.assertEqual(facts["daily"]["trade_state_issues"]["trading_without_turnover"], 1)
            self.assertEqual(facts["stock_info"]["rows"], 2)
            self.assertEqual(facts["valuation"]["rows"], 1)
            self.assertEqual(facts["fundamentals"]["rows"], 1)
            self.assertIn("pe_ttm", facts["valuation"]["field_stats"])
            self.assertIn("roe", facts["fundamentals"]["field_stats"])
            profile = bus.profile_report()
            self.assertIn("pe_ttm", profile["估值因子字段"])
            self.assertIn("roe", profile["基本面因子字段"])
            self.assertNotEqual(profile["日线字段"]["open"]["有值率"], 100.0)
            self.assertNotEqual(profile["总览"]["股票主档"]["行数"], 0)
            self.assertNotEqual(profile["结论"]["级别"], "✅")
            self.assertNotIn("close_hfq", " ".join(profile["异常检查"]))
            inputs = bus.research_report()
            self.assertEqual(inputs["freshness"]["latest_date"], "2025-01-03")
            self.assertEqual(inputs["freshness"]["latest_stock_count"], 1)
            self.assertEqual(inputs["duplicate_keys"], 1)
            self.assertEqual(len(inputs["price_basis_issues"]), 0)
            self.assertEqual(len(inputs["unverified_code_changes"]), 1)
            self.assertEqual(inputs["saved_factor_files"], 1)
            self.assertEqual(inputs["factor_cache_check"]["check_scope"], "all")
            bus.write("sources", "registry", "sources.json", json.dumps({"sources": [{
                "id": "daily_fix", "name": "Daily fix", "method": "fixture",
                "frequency": "daily", "data_type": "daily", "modes": ["fix"],
                "default": True, "defaults": {}
            }]}).encode())
            doctor = bus.doctor_report()
            self.assertTrue(any(item["可修复"] and item["fix"]["source_id"] == "daily_fix"
                                for item in doctor["items"]))

    def test_daily_identity_normalization_deduplicates_aliases_and_keeps_markets(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from datetime import date
        from protocal import DataQuery, normalize_security_id

        self.assertEqual(normalize_security_id("600036"), "sh.600036")
        self.assertEqual(normalize_security_id("sh.600036"), "sh.600036")
        self.assertNotEqual(normalize_security_id("sh.000001"), normalize_security_id("sz.000001"))
        self.assertEqual(normalize_security_id("sh.000001"), "sh.000001")

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bus = DataBus(DataStore(root))
            calendar = bus.write("sources", "market", "calendar.json",
                                 json.dumps({"sessions": ["2026-01-02", "2026-01-05"]}).encode())
            bus.write("sources", "market", "manifest.json", json.dumps({
                "schema": 2, "files": [{"file": "calendar.json", "sha256": calendar.sha256,
                                         "role": "market"}], "fields": {},
                "scope": {"symbols": ["600036", "sh.000001", "sz.000001"],
                          "universe": "market"}, "calendar": "calendar.json"}).encode())
            row = {"date": "2026-01-02", "open": 10.0, "high": 11.0,
                   "low": 9.0, "close": 10.5, "volume": 100.0, "amount": 1050.0}
            packet = {"calendar": ["2026-01-02", "2026-01-05"], "records": [
                {**row, "code": "600036"}, {**row, "code": "sh.600036"},
                {**row, "code": "sh.000001"}, {**row, "code": "sz.000001"}],
                "fields": ["open", "high", "low", "close", "volume", "amount"],
                "scope": {"universe": "market"}}
            Methods(bus).publish({'id': 'market', 'data_type': 'daily',
                                  'storage_id': 'lake', 'dataset_id': 'market'}, {**packet, 'raw': []})
            dataset = bus.query(DataQuery(
                "market", date(2026, 1, 2), date(2026, 1, 2),
                ("open", "high", "low", "close", "volume", "amount"), 1, "raw",
                symbols=("sh.600036", "sh.000001", "sz.000001"), universe="market"))
            self.assertEqual(set(dataset.records[0].bars),
                             {"sh.600036", "sh.000001", "sz.000001"})
            self.assertEqual(len(dataset.records[0].bars), 3)

    def test_cancel_stops_before_publish_and_close_stops_scheduler(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            entered, release = threading.Event(), threading.Event()
            published = []
            def handler(source, request):
                entered.set()
                release.wait(2)
                return {"records": [], "raw": [], "scope": {"category": "generic"}}
            methods = Methods(bus, {"fixture": handler})
            original = methods.publish
            methods.publish = lambda source, packet: published.append(original(source, packet))
            collect = Collect(sources, methods)
            result = []
            thread = threading.Thread(target=lambda: result.append(
                collect.run({"source_id": "fixture"})))
            thread.start()
            self.assertTrue(entered.wait(1))
            job_id = next(iter(collect._jobs))
            collect.cancel(job_id)
            release.set()
            thread.join(2)
            self.assertEqual(result[0]["status"], "failed")
            self.assertEqual(published, [])
            collect.start()
            collect.close()

    def test_collect_releases_active_when_initial_save_fails(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [], "raw": [], "scope": {"kind": "fixture"}}})
            collect = Collect(sources, methods)
            collect._save = lambda _job: (_ for _ in ()).throw(OSError("save failed"))
            result = collect.run({"source_id": "fixture"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(collect._active, set())

    def test_collect_log_failure_does_not_change_successful_publish(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {},
                              "params": {"key_fields": ["value"]}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [{"value": 1}], "raw": [], "scope": {"kind": "fixture"}}})
            methods.log = lambda _event: (_ for _ in ()).throw(OSError("log failed"))
            result = Collect(sources, methods).run({"source_id": "fixture"})
            self.assertEqual(result["status"], "succeeded")
            self.assertIn("log_error", result)

    def test_enabled_schedule_runs_through_collect_run(self):
        from collect.core import Collect
        from collect.methods import Methods
        from collect.sources import Sources

        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({"id": "fixture", "name": "Fixture", "method": "fixture",
                              "frequency": "daily", "defaults": {},
                              "params": {"key_fields": ["identity"]}})
            methods = Methods(bus, {"fixture": lambda source, request: {
                "records": [], "raw": [], "scope": {"kind": "fixture"}}})
            collect = Collect(sources, methods)
            collect.schedule("fixture", {"enabled": True, "next_at": 0,
                                          "interval_seconds": 60, "input": {}})
            collect.tick(at=1)
            jobs = [value for value in collect._jobs.values() if value["source_id"] == "fixture"]
            self.assertEqual(len(jobs), 0)
            schedule = json.loads(bus.read("state", "collect", "schedules/fixture.json"))
            collect.tick(at=schedule["next_at"])
            jobs = [value for value in collect._jobs.values() if value["source_id"] == "fixture"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "succeeded")

    def test_minute_quotes_use_time_keys_and_minute_fields(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        names = ['date', 'time', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adjustflag']
        class Rows:
            error_code = '0'; error_msg = ''; fields = names
            def next(self): return False
        query = []
        def collect(*args, **kwargs):
            query.append(args[1])
            return Rows()
        provider = SimpleNamespace(login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
                                   query_history_k_data_plus=collect)
        with patch.dict('sys.modules', {'baostock': provider}):
            collect_daily({'codes': ['sh.600000'], 'start': '2026-10-01', 'end': '2026-10-02', 'frequency': '5'})
        self.assertEqual(query, [','.join(names)])

    def test_quote_values_must_be_finite_numbers(self):
        from collect.methods import EastmoneyQuotes, CollectionError
        import copy
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        for field, value in (('f2', '-'), ('f3', float('nan')), ('f5', -1), ('f6', True), ('f124', False)):
            response = copy.deepcopy(fixture['response'])
            response['data']['diff'][0][field] = value
            with self.subTest(field=field), self.assertRaises(CollectionError):
                EastmoneyQuotes({}, lambda *a: response).collect(fixture['request'])

    def test_security_lists_return_evidence_without_own_files(self):
        from collect.methods import Securities
        from types import SimpleNamespace
        import pandas as pd
        provider = SimpleNamespace(
            stock_info_sh_delist=lambda **_: pd.DataFrame([{'公司代码': '600001', '公司简称': '甲', '上市日期': '2000-01-01', '暂停上市日期': '2026-10-01'}]),
            stock_info_sz_delist=lambda **_: pd.DataFrame([{'证券代码': '000001', '证券简称': '乙', '上市日期': '2001-01-01', '终止上市日期': '2026-10-02'}]))
        result = Securities({}, provider).collect({'start': '2026-10-01', 'end': '2026-10-01', 'codes': []})
        self.assertEqual([row['bs_code'] for row in result['records']], ['sh.600001'])
        self.assertEqual(len(result['raw']), 2)

    def test_security_list_matches_bare_public_code_by_canonical_identity(self):
        from collect.methods import Securities
        import pandas as pd
        provider = SimpleNamespace(
            stock_info_sh_delist=lambda **_: pd.DataFrame([{
                '公司代码': '600036', '公司简称': '招商银行',
                '上市日期': '2000-01-01', '暂停上市日期': '2026-10-01'}]),
            stock_info_sz_delist=lambda **_: pd.DataFrame(columns=[
                '证券代码', '证券简称', '上市日期', '终止上市日期']))
        result = Securities({}, provider).collect({
            'start': '2026-10-01', 'end': '2026-10-01', 'codes': ['600036']})
        self.assertEqual([row['bs_code'] for row in result['records']], ['sh.600036'])

    def test_news_uses_current_records_and_skips_existing_dates(self):
        from collect.methods import News, Methods
        from collect.core import Collect
        from collect.sources import Sources
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><article class="content-section"><h2>标题</h2><div class="content-body"><p>正文<a href="/">链接</a>结尾</p></div></article>'
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            sources = Sources(bus)
            sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                              'frequency': 'daily', 'defaults': {}})
            calls = []
            methods = Methods(bus, {'news': News({}, lambda url: calls.append(url) or page)})
            collect = Collect(sources, methods)
            request = {'source_id': 'news', 'start': '2026-10-01', 'end': '2026-10-01', 'codes': []}
            job = collect.run(request)
            self.assertEqual(job['status'], 'succeeded', job['error'])
            snapshot = methods.records(sources.get('news'))
            self.assertEqual(snapshot['records'][0]['articles'][0]['content'], '正文链接结尾')
            self.assertEqual(snapshot['raw'][0]['response'], page)
            self.assertEqual(collect.run(request)['status'], 'succeeded')
            self.assertEqual(len(calls), 1)
            self.assertEqual(methods.records(sources.get('news'))['total'], 1)
            self.assertEqual(list(Path(root).rglob('*.db')), [])

    def test_default_news_http_and_invalid_pages(self):
        from collect.methods import News, DEFAULT_METHODS, CollectionError
        self.assertIs(DEFAULT_METHODS.get('news'), News)
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><nav><a href="#section-1">标题</a></nav><article class="content-section" id="section-1"><h2 id="1">标题</h2><div class="content-body"><p>正文</p></div></article>'
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = page.encode()
        with patch('collect.methods.urlopen', return_value=response):
            packet = News({}).collect({'start': '2026-10-01', 'end': '2026-10-01', 'interval': 0})
        self.assertEqual(packet['records'][0]['url'], 'https://cn.govopendata.com/xinwenlianbo/20261001/')
        for bad in (page.replace('10月1日', '10月2日'), page.replace('<p>正文</p>', '<p></p>'),
                    page.replace('href="#section-1"', 'href="#section-2"'),
                    page.replace('<nav>', '<div class="toc">').replace('</nav>', '<a href="#missing">缺失</a></div>'),
                    page.replace('</nav>', '<a href="#section-1">重复</a></nav>')):
            with self.subTest(page=bad), self.assertRaises(CollectionError):
                News({}, lambda url: bad).collect({'start': '2026-10-01', 'end': '2026-10-01'})
        from urllib.error import HTTPError
        def missing(url):
            raise HTTPError(url, 404, 'not published', {}, None)
        with self.assertRaises(CollectionError):
            News({}, missing).collect({'start': '2026-10-01', 'end': '2026-10-01'})

    def test_news_schedule_uses_manual_engine_and_is_disabled_by_default(self):
        from collect.methods import News
        from collect.core import Collect
        from collect.sources import Sources
        page = '<h1 class="article-title">2026年10月1日 新闻联播</h1><article class="content-section"><h2>标题</h2><div class="content-body"><p>正文</p></div></article>'
        calls = []
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); sources = Sources(bus)
            source = sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                                       'frequency': 'daily', 'defaults': {}})
            methods = Methods(bus, {'news': News({}, lambda url: calls.append(url) or page)})
            collect = Collect(sources, methods)
            settings = {'input': {'start': '2026-10-01', 'end': '2026-10-01'}, 'interval_seconds': 60}
            self.assertFalse(collect.schedule('news', settings)['enabled'])
            collect.tick(time.time() + 61)
            self.assertEqual(calls, [])
            schedule = collect.schedule('news', {**settings, 'enabled': True})
            collect.tick(schedule['next_at'])
            self.assertEqual(methods.records(source)['total'], 1)
            collect.tick(schedule['next_at'] + 61)
            self.assertEqual(len(calls), 1)
            collect.close()

    def test_news_missing_dates_and_conflicts_keep_full_content(self):
        from collect.methods import News, CollectionError
        from collect.sources import Sources
        from collect.core import Collect
        calls = []
        def transport(url):
            calls.append(url)
            number = int(url.rstrip('/').split('/')[-1][-2:])
            return f'<h1 class="article-title">2026年10月{number}日 新闻联播</h1><article class="content-section"><h2>新闻</h2><div class="content-body"><p>全文</p></div></article>'
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root))); sources = Sources(bus)
            source = sources.register({'id': 'news', 'name': 'News', 'method': 'news',
                                       'frequency': 'daily', 'defaults': {}})
            methods = Methods(bus, {'news': News({}, transport)})
            with self.assertRaises(ValueError):
                methods.publish(source, {'records': [{'url': 'broken', 'date': '2026-10-01', 'articles': []}],
                                         'raw': [], 'scope': {}})
            collect = Collect(sources, methods)
            for day in ('01', '03'):
                self.assertEqual(collect.run({'source_id': 'news', 'start': f'2026-10-{day}',
                                             'end': f'2026-10-{day}'})['status'], 'succeeded')
            self.assertEqual(collect.run({'source_id': 'news', 'start': '2026-10-01',
                'end': '2026-10-03', 'keyword': '不匹配', 'fields': ['date']})['status'], 'succeeded')
            self.assertEqual([url[-3:-1] for url in calls], ['01', '03', '02'])
            self.assertEqual(methods.records(source)['total'], 3)
            self.assertEqual(methods.records(source)['records'][0]['articles'][0]['content'], '全文')
            before = bus.read('sources', 'news', 'records.json')
            row = methods.records(source)['records'][0]
            row['articles'][0]['content'] = '篡改'
            with self.assertRaises(DataError):
                methods.publish(source, {'records': [row], 'raw': [], 'scope': {}})
            self.assertEqual(bus.read('sources', 'news', 'records.json'), before)
            count = len(calls)
            for params in ({'timeout': 0}, {'interval': -1}, {'start': '2026-10-04', 'end': '2026-10-01'}):
                with self.assertRaises(ValueError):
                    News({}, transport).collect({'start': '2026-10-01', 'end': '2026-10-01', **params})
            self.assertEqual(len(calls), count)

    def test_latest_quote_fields_are_selectable(self):
        from collect.methods import EastmoneyQuotes
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        result = EastmoneyQuotes({}, lambda *a: fixture['response']).collect({**fixture['request'], 'fields': ['price', 'volume']})
        self.assertEqual(set(result['records'][0]), {'code', 'data_at', 'raw_page', 'price', 'volume'})

    def test_latest_quote_rejects_unsupported_parameters(self):
        from collect.methods import EastmoneyQuotes, CollectionError
        fixture = json.loads((Path(__file__).parent / 'fixtures/collect-quotes-ab.json').read_bytes())
        from unittest.mock import Mock
        transport = Mock(return_value=fixture['response'])
        provider = EastmoneyQuotes({}, transport)
        for extra in ({'fields': []}, {'fields': ['unknown']}, {'frequency': 'w'}, {'adjustment': '2'}):
            with self.subTest(extra=extra), self.assertRaises(CollectionError):
                provider.collect({**fixture['request'], **extra})
        transport.assert_not_called()

    def test_daily_parameters_reach_the_provider(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls=[]
        class Rows:
            error_code='0'; error_msg=''; fields=['date','code','open','high','low','close','volume','amount']
            def next(self): return False
        provider=SimpleNamespace(login=lambda:SimpleNamespace(error_code='0'), logout=lambda:None,
            query_history_k_data_plus=lambda *args,**kwargs: calls.append((args,kwargs)) or Rows())
        with patch.dict('sys.modules', {'baostock':provider}):
            collect_daily({'codes':['sh.600000'],'start':'2026-09-01','end':'2026-09-30',
                           'frequency':'w','adjustment':'2','fields':Rows.fields})
        self.assertEqual(calls[0][1]['frequency'], 'w')
        self.assertEqual(calls[0][1]['adjustflag'], '2')
        self.assertEqual(calls[0][0][1], ','.join(Rows.fields))

    def test_daily_provider_maps_public_security_ids_and_adds_sdk_identity_fields(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        class Rows:
            error_code = '0'; error_msg = ''
            fields = ['date', 'code', 'close']
            def next(self): return False
        class TradeDates:
            error_code = '0'; error_msg = ''
            fields = ['calendar_date', 'is_trading_day']
            def __init__(self): self.rows = [['2026-09-30', '1'], ['2026-10-01', '1']]
            def next(self): return bool(self.rows)
            def get_row_data(self): return self.rows.pop(0)
        provider = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
            query_trade_dates=lambda *args, **kwargs: TradeDates(),
            query_history_k_data_plus=lambda *args, **kwargs: calls.append((args, kwargs)) or Rows())
        with patch.dict('sys.modules', {'baostock': provider}):
            collect_daily({'codes': ['600036', 'sh.000001'],
                           'start': '2026-09-01', 'end': '2026-09-30',
                           'fields': ['close']})
        self.assertEqual([call[0][0] for call in calls], ['sh.600036', 'sh.000001'])
        self.assertEqual(calls[0][0][1], 'date,code,close')

    def test_daily_invalid_fields_are_rejected_before_provider_login(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        provider = SimpleNamespace(
            login=lambda: calls.append("login") or SimpleNamespace(error_code='0'),
            logout=lambda: calls.append("logout"),
        )
        with patch.dict('sys.modules', {'baostock': provider}):
            with self.assertRaises(ValueError):
                collect_daily({'codes': ['sh.600000'], 'start': '2026-09-01',
                               'end': '2026-09-30', 'fields': ['date', 'unknown']})
        self.assertEqual(calls, [])

    def test_daily_calendar_end_is_rejected_before_provider_login(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        calls = []
        provider = SimpleNamespace(
            login=lambda: calls.append("login") or SimpleNamespace(error_code='0'),
            logout=lambda: calls.append("logout"),
        )
        with patch.dict('sys.modules', {'baostock': provider}):
            for calendar_end in ('not-a-date', '2026-08-31'):
                with self.subTest(calendar_end=calendar_end), self.assertRaises(ValueError):
                    collect_daily({'codes': ['sh.600000'], 'start': '2026-09-01',
                                   'end': '2026-09-30', 'calendar_end': calendar_end,
                                   'fields': ['close']})
        self.assertEqual(calls, [])

    def test_trade_calendar_reports_provider_error_before_field_validation(self):
        from collect.methods import BaostockDaily, ProviderError
        class Response:
            error_code = '1001'
            error_msg = 'calendar unavailable'
            fields = []
        with self.assertRaisesRegex(ProviderError, 'calendar unavailable'):
            BaostockDaily.trade_calendar(
                SimpleNamespace(query_trade_dates=lambda **kwargs: Response()),
                '2026-10-13', '2026-10-14')

    def test_daily_can_collect_status_without_price_fields(self):
        from collect.methods import collect_daily
        from types import SimpleNamespace
        class Rows:
            error_code = '0'; fields = ['date', 'code', 'isST', 'volume', 'tradestatus']
            def next(self): return False
        class TradeDates:
            error_code = '0'; error_msg = ''
            fields = ['calendar_date', 'is_trading_day']
            def __init__(self): self.rows = [['2026-10-01', '1'], ['2026-10-02', '1'], ['2026-10-03', '1']]
            def next(self): return bool(self.rows)
            def get_row_data(self): return self.rows.pop(0)
        provider = SimpleNamespace(login=lambda: SimpleNamespace(error_code='0'), logout=lambda: None,
                                   query_trade_dates=lambda *args, **kwargs: TradeDates(),
                                   query_history_k_data_plus=lambda *args, **kwargs: Rows())
        with patch.dict('sys.modules', {'baostock': provider}):
            result = collect_daily({'codes': ['sz.301001'], 'start': '2026-10-01', 'end': '2026-10-02', 'fields': Rows.fields})
        self.assertEqual(result['records'], [])
