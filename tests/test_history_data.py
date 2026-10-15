from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from protocal import DataBus, DataError, DataQuery
from storage import DataStore
from tests.history_fixture import make_snapshot

FIELDS = ("open", "high", "low", "close", "volume", "amount", "signal_close")


def query(root, *, fields=FIELDS, symbols=("600003",), history=20,
          start=date(2020, 1, 31), end=date(2020, 2, 4), universe="dataset"):
    return DataBus(DataStore(root)).query(
        DataQuery("fixture", start, end, fields, history, "adjusted", symbols=symbols, universe=universe)
    )


def payload(**changes):
    value = {
        "run_id": "history-test", "module": "plugins.strategies.breadth_low_amount_reversal",
        "params": {"positions": 4}, "initial_cash": "100000", "dataset_id": "fixture",
        "start": "2020-01-31", "end": "2020-02-04",
        "limits": {"stock_weight": "0.4", "adv_fraction": "0.01"},
        "execution": {
            "price": {"model": "ratio", "params": {"rate": ".001", "tick": ".01"}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": ".01", "min_qty": 100, "step": 100}},
        },
    }
    value.update(changes)
    return value


class HistoricalDataTests(unittest.TestCase):
    def test_raw_query_reuses_trade_state_validation(self):
        import pyarrow.parquet as pq
        from protocal import DataBus, DataQuery

        cases = ((0.0, 100.0, 1000.0, "contradictory_trade_state"),
                 (1.0, 0.0, 0.0, "contradictory_trade_state"),
                 (None, 0.0, 0.0, "unknown_trade_state"))
        for status, volume, amount, reason in cases:
            with self.subTest(status=status, volume=volume, amount=amount):
                with TemporaryDirectory() as temp:
                    root = Path(temp); make_snapshot(root)
                    path = root / "data/source/fixture/daily/bars.parquet"
                    table = pq.read_table(path); rows = table.to_pylist()
                    for row in rows:
                        if row["code"] == "600003" and row["date"] == "2020-01-31":
                            row.update(tradestatus=status, volume=volume, amount=amount)
                    pq.write_table(table.from_pylist(rows, schema=table.schema), path)
                    with self.assertRaisesRegex(ValueError, reason):
                        DataBus(DataStore(root)).query(DataQuery(
                            "fixture", date(2020, 1, 31), date(2020, 1, 31),
                            ("open", "high", "low", "close", "volume", "amount"),
                            1, "raw", symbols=("sh.600003",), universe="CN_A_SHARE"))

    def test_raw_query_keeps_a_zero_turnover_suspension_as_valuation_only(self):
        import pyarrow.parquet as pq
        from protocal import DataBus, DataQuery

        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path); rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] == "2020-01-31":
                    row.update(tradestatus=0.0, volume=0.0, amount=0.0)
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            dataset = DataBus(DataStore(root)).query(DataQuery(
                "fixture", date(2020, 1, 31), date(2020, 1, 31),
                ("open", "high", "low", "close", "volume", "amount"),
                1, "raw", symbols=("sh.600003",), universe="CN_A_SHARE"))
            record = dataset.records[0]
            self.assertNotIn("600003", record.bars)
            self.assertEqual(record.frame.fields["sh.600003"]["close"], 11.0)

    def test_history_day_exposes_prior20_raw_volume_and_empty_first_value(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.records[0].opening_volume20, {})
            volumes = [record.bars["600003"].volume for record in dataset.records[:20]]
            self.assertEqual(dataset.records[20].opening_volume20["600003"], Decimal(str(sum(volumes) / 20)))

    def test_adjusted_query_normalizes_a_market_qualified_symbol(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            dataset = query(root, symbols=("sh.600003",))
            self.assertEqual(dataset.scope["symbols"], ("sh.600003",))
            self.assertIn("sh.600003", dataset.records[-1].bars)

    def test_history_day_keeps_amount_liquidity_separate_from_volume_liquidity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            amount = [record.bars["600003"].amount for record in dataset.records[:20]]
            volume = [record.bars["600003"].volume for record in dataset.records[:20]]
            self.assertEqual(dataset.records[20].opening_amount20["600003"], Decimal(str(sum(amount) / 20)))
            self.assertEqual(dataset.records[20].opening_volume20["600003"], Decimal(str(sum(volume) / 20)))
            self.assertNotEqual(dataset.records[20].opening_amount20["600003"], dataset.records[20].opening_volume20["600003"])

    def test_suspension_keeps_last_amount_and_volume_liquidity_for_resume(self):
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"; table = pq.read_table(path)
            rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] == "2020-01-31":
                    row.update(tradestatus=0.0, volume=0.0, amount=0.0)
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            dataset = query(root); by_day = {record.trade_date: record for record in dataset.records}
            suspended, resumed = by_day[date(2020, 1, 31)], by_day[date(2020, 2, 3)]
            previous = [record for record in dataset.records if record.trade_date < date(2020, 1, 31)
                        and record.bars["600003"].volume > 0]
            self.assertEqual(suspended.bars["600003"].volume, 0.0)
            self.assertEqual(suspended.bars["600003"].amount, 0.0)
            self.assertEqual(resumed.opening_amount20["600003"], Decimal(str(sum(r.bars["600003"].amount for r in previous[-20:]) / 20)))
            self.assertEqual(resumed.opening_volume20["600003"], Decimal(str(sum(r.bars["600003"].volume for r in previous[-20:]) / 20)))

    def test_history_context_exposes_only_liquidity_input_basis(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.provenance["query_semantics"], "snapshot_replay")
            self.assertNotIn("execution", dataset.provenance)
            self.assertNotIn("execution_hash", dataset.provenance)

    def test_volume_warmup_rejects_missing_observations_before_query(self):
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            path = root / "data/source/fixture/daily/bars.parquet"; table = pq.read_table(path); rows = table.to_pylist()
            for row in rows:
                if row["code"] == "600003" and row["date"] < "2020-01-31": row["volume"] = None
            pq.write_table(table.from_pylist(rows, schema=table.schema), path)
            with self.assertRaisesRegex(ValueError, "volume"): query(root)

    def test_preflight_effective_fields_match_resolved_dataset_fields(self):
        from protocal import KernelBacktestPort
        from tests.fee_fixture import configured_settlement
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            port = KernelBacktestPort(DataBus(DataStore(root)), configured_settlement())
            result = port.handle("workbench.preflight", payload(
                module="plugins.strategies.moving_average_cross",
                params={"fast_window": 2, "slow_window": 3}, symbols=["600000"],
                fields=["signal_close"], history=3))
            self.assertFalse(result["issues"])
            self.assertEqual(result["requirements"]["fields"], ["signal_close"])
            self.assertIn("signal_close", result["effective_config"]["fields"])

    def test_real_snapshot_without_universe_source_file_is_readable(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root)
            self.assertEqual(dataset.scope["symbols"], ("600003",))
            self.assertNotIn("universe_source", dataset.scope)

    def test_verified_boolean_does_not_authorize_a_dataset(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); folder = root / "data/source/sample"; folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps({"verified": True, "schema": 1}))
            with self.assertRaises(DataError):
                DataBus(DataStore(root)).query(DataQuery("sample", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted"))

    def test_unknown_dataset_is_explicit_failure(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(DataError):
                DataBus(DataStore(Path(temp))).query(DataQuery("missing", date(2020, 1, 1), date(2020, 1, 2), ("close",), 1, "adjusted"))

    def test_real_files_execute_parameter_ab_with_one_strategy_file(self):
        from plugins.data import Settlement
        from protocal import KernelBacktestPort
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            port = KernelBacktestPort(DataBus(DataStore(root)), Settlement)
            a = port.handle("workbench.backtest", payload(run_id="0", params={"positions": 4})).data
            b = port.handle("workbench.backtest", payload(
                run_id="1", params={"positions": 8},
                execution={**payload()["execution"], "price": {"model": "ratio", "params": {"rate": ".002", "tick": ".01"}}},
            )).data
            self.assertTrue(a["trades"]); self.assertTrue(b["trades"])
            self.assertEqual(a["data_hash"], b["data_hash"])
            self.assertNotEqual(a["execution_hash"], b["execution_hash"])
            self.assertNotEqual(a["metrics"]["final_equity"], b["metrics"]["final_equity"])

    def test_parameter_change_uses_same_account_policy(self):
        from plugins.data import Settlement
        from protocal import KernelBacktestPort
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root)
            result = KernelBacktestPort(DataBus(DataStore(root)), Settlement).handle("workbench.preflight", payload(params={"positions": 8}))
            self.assertFalse(result["issues"])

    def test_long_history_stays_bounded_and_trace_does_not_duplicate_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root, history_start=date(2018, 1, 1)); dataset = query(root)
            self.assertGreater(len(dataset.records), 500)
            self.assertLessEqual(max(len(rows) for record in dataset.records for rows in record.frame.history.values()), 20)

    def test_market_source_cannot_be_excluded_using_account_role(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(path.read_text()); manifest["files"][0]["role"] = "account"; path.write_text(json.dumps(manifest))
            self.assertEqual(query(root).dataset, "fixture")

    def test_unseen_strategy_requests_subset_and_only_declared_fields(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); dataset = query(root, history=17)
            self.assertEqual(tuple(dataset.scope["symbols"]), ("600003",))
            self.assertTrue(all(set(row.fields) == set(FIELDS) for row in dataset.records[-1].frame.history["600003"]))

    def test_declared_arbitrary_numeric_field_is_aligned_and_not_substituted(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/daily/bars.parquet"
            table = pq.read_table(path).append_column("arbitrary_value", pa.array([i + 0.5 for i in range(pq.read_table(path).num_rows)]))
            pq.write_table(table, path); manifest_path = root / "data/source/fixture/manifest.json"
            manifest = json.loads(manifest_path.read_text()); manifest["fields"] = {"quality": {"column": "arbitrary_value", "unit": "研究分数"}}
            manifest_path.write_text(json.dumps(manifest)); dataset = query(root, fields=("quality",))
            self.assertIsInstance(dataset.records[-1].frame.fields["600003"]["quality"], float)

    def test_market_universe_requirement_rejects_fixed_dataset_and_reserved_fields(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root); path = root / "data/source/fixture/manifest.json"; manifest = json.loads(path.read_text())
            manifest["scope"]["universe"] = "fixed_symbols"; path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "universe_scope_mismatch"):
                query(root, symbols=None, universe="CN_A_SHARE")
            manifest["scope"]["universe"] = "CN_A_SHARE"; manifest["fields"] = {"signal_close": {"column": "close", "unit": "元"}}
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "reserved"): query(root)

    def test_execution_history_requirement_is_checked_before_short_strategy_run(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); make_snapshot(root, history_start=date(2020, 1, 1))
            with self.assertRaisesRegex(ValueError, "requires 20"): query(root, history=2, start=date(2020, 1, 10))


if __name__ == "__main__":
    unittest.main()
