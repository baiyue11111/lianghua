"""Contract tests for event-time BaoStock signal prices over raw lake bars."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from collect.data import AdjustmentCoverageError, load_adjusted_lake_panel


FACTOR_START = date(1990, 1, 1)
FACTOR_END = date(2026, 9, 18)
FIELDS = ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _bar(day: str, *, close: float, open_: float, high: float, low: float,
         code: str = "600000", amount: float = 100_000_000,
         volume: float | None = None, tradestatus: float | None = 1.0) -> dict:
    return {"code": code, "date": day, "name": "浦发银行", "open": open_,
            "high": high, "low": low, "close": close,
            "amount": amount, "pool": "current", "tradestatus": tradestatus,
            "volume": amount / abs(close) if volume is None else volume}


class AdjustedPanelTests(unittest.TestCase):
    def test_factor_failure_does_not_hide_independent_raw_bar_failures(self):
        bad_price=_bar('2020-01-02',close=-10,open_=10,high=11,low=9)
        bad_status=_bar('2020-01-02',close=10,open_=10,high=11,low=9,tradestatus=0)
        bad_status['code']='600001'
        self.write_bars([bad_price,bad_status])
        self.write_cache([['sh.600001','2020-01-02','1','1','1']],code='sh.600001')
        with self.assertRaises(AdjustmentCoverageError) as caught: self.load()
        reasons={(issue.code,issue.reason) for issue in caught.exception.issues}
        self.assertIn(('600000','missing_factor_cache'),reasons)
        self.assertIn(('600000','invalid_lake_price'),reasons)
        self.assertIn(('600001','contradictory_trade_state'),reasons)
        self.assertEqual(caught.exception.checked_bars,2)

    def test_verified_rename_uses_old_alias_factor_history(self):
        self.write_bars([
            _bar('2019-12-30',close=10,open_=10,high=11,low=9),
            _bar('2020-01-02',close=11,open_=11,high=12,low=10)])
        self.write_cache([['sh.600001','2019-01-01','1','2','2']],code='sh.600001')
        self.write_cache([['sh.600000','2020-01-02','1','2','2']])
        self.code_changes.write_text(json.dumps({'schema':1,'events':[
            {'old_code':'600001','new_code':'600000','effective_date':'2020-01-02',
             'factor_basis_continuity_verified':True}]}))
        panel = self.load()
        np.testing.assert_allclose(panel.back_factor[0],[2,2])
        np.testing.assert_allclose(panel.signal_close[0],[20,22])
        self.assertTrue(any('adjust_600001_' in h.path for h in panel.source_hashes))

    def test_rename_boolean_cannot_authorize_a_contradictory_factor(self):
        self.write_bars([_bar('2019-12-30',close=10,open_=10,high=11,low=9),
                         _bar('2020-01-02',close=11,open_=11,high=12,low=10)])
        self.write_cache([['sh.600001','2019-01-01','1','2','2']],code='sh.600001')
        self.write_cache([['sh.600000','2020-01-02','1','1','1']])
        self.code_changes.write_text(json.dumps({'schema':1,'events':[
            {'old_code':'600001','new_code':'600000','effective_date':'2020-01-02',
             'factor_basis_continuity_verified':True}]}))
        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()
        self.assertIn('invalid_factor_history',{issue.reason for issue in caught.exception.issues})

    def test_mixed_price_basis_cannot_be_adjusted_twice(self):
        self.write_bars([_bar("2020-01-02", close=150, open_=150,
                             high=155, low=145, amount=10_000, volume=1000)])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])
        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()
        self.assertIn("inconsistent_price_basis", {i.reason for i in caught.exception.issues})
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.daily = self.root / "daily"
        self.cache = self.root / "cache"
        self.stock_basic = self.root / "stock_basic"
        self.code_changes = self.root / "code_changes.json"
        self.daily.mkdir()
        self.cache.mkdir()
        self.stock_basic.mkdir()
        self.code_changes.write_text(json.dumps({"schema": 1, "scope": "test", "events": []}),
                                     encoding="utf-8")

    def write_bars(self, rows: list[dict]) -> Path:
        path = self.daily / "2020.parquet"
        pq.write_table(pa.Table.from_pylist(rows), path)
        return path

    def write_cache(self, rows: list[list[str]], *, code: str = "sh.600000",
                    status: str | None = None) -> Path:
        request = {"kind": "adjust", "code": code, "start": FACTOR_START.isoformat(),
                   "end": FACTOR_END.isoformat(), "year": None}
        raw = {"error_code": "0", "error_msg": "success", "code": code,
               "fields": FIELDS, "rows": rows}
        payload = {"schema": 2, "status": status or ("ok" if rows else "empty"),
                   "request": request, "request_sha256": _sha256_json(request),
                   "request_fields_sha256": hashlib.sha256(",".join(FIELDS).encode()).hexdigest(),
                   "raw_response": raw, "raw_response_sha256": _sha256_json(raw),
                   "collected_at": "2026-09-19T00:00:00+0800"}
        path = self.cache / f"adjust_{code.split('.')[1]}_{FACTOR_START}_{FACTOR_END}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_stock_basic(self, *, code: str = "sh.600000",
                          ipo_date: str = "2020-01-02") -> Path:
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        raw = {"error_code": "0", "error_msg": "success", "fields": fields,
               "rows": [[code, "浦发银行", ipo_date, "", "1", "1"]]}
        payload = {"schema": 1, "request": f"query_stock_basic({code})",
                   "raw_response": raw, "raw_response_sha256": _sha256_json(raw),
                   "collected_at": "2026-09-24T00:00:00+00:00"}
        path = self.stock_basic / f"stock_basic_{code}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def load(self):
        return load_adjusted_lake_panel(
            self.daily, self.cache, factor_start=FACTOR_START, factor_end=FACTOR_END,
            stock_basic_dir=self.stock_basic, code_changes_path=self.code_changes,
            read=lambda path: path.read_bytes(),
            list_files=lambda directory, pattern: tuple(directory.glob(pattern)))

    def test_factor_is_effective_on_event_day_for_signals_only(self):
        # Regressions caught: future factor leaks into yesterday, or execution
        # cash prices are silently replaced with adjusted signal prices.
        daily_path = self.write_bars([
            _bar("2020-01-02", close=10, open_=9.8, high=11, low=9.5),
            _bar("2020-01-03", close=11, open_=10.5, high=12, low=10),
            _bar("2020-01-06", close=5.5, open_=5.2, high=6, low=5),
        ])
        cache_path = self.write_cache([
            ["sh.600000", "2020-01-02", "0.5", "1", "1"],
            ["sh.600000", "2020-01-06", "1", "2", "2"],
        ])

        panel = self.load()

        self.assertEqual(panel.symbols, ("600000",))
        self.assertEqual(panel.calendar, (date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)))
        np.testing.assert_allclose(panel.back_factor[0], [1, 1, 2])
        np.testing.assert_allclose(panel.signal_close[0], [10, 11, 11])
        np.testing.assert_allclose(panel.signal_high[0], [11, 12, 12])
        np.testing.assert_allclose(panel.raw_open[0], [9.8, 10.5, 5.2])
        np.testing.assert_allclose(panel.raw_close[0], [10, 11, 5.5])
        np.testing.assert_allclose(panel.amount[0], [100_000_000] * 3)
        hashes = {entry.path: entry.sha256 for entry in panel.source_hashes}
        self.assertEqual(hashes[str(daily_path)], hashlib.sha256(daily_path.read_bytes()).hexdigest())
        self.assertEqual(hashes[str(cache_path)], hashlib.sha256(cache_path.read_bytes()).hexdigest())

    def test_first_bar_before_first_event_is_not_filled_with_one(self):
        # Regressions caught: a successful cache query is mistaken for proof of
        # the pre-event anchor, or a first factor row of 1 is back-filled.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10)])
        self.write_cache([["sh.600000", "2020-01-03", "0.9", "1", "1"]])

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "missing_stock_basic_snapshot"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_tampered_provider_response_is_not_trusted(self):
        # Regressions caught: accepting status=ok while the saved response has
        # changed after its original provider hash was recorded.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10)])
        path = self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["raw_response"]["rows"][0][3] = "2"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "invalid_factor_cache"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_verified_per_code_ipo_snapshot_supplies_listing_anchor(self):
        # Regressions caught: rejecting a proven listing anchor, or applying
        # the first future factor to the IPO bar rather than factor one.
        self.write_bars([
            _bar("2020-01-02", close=10, open_=10, high=11, low=9),
            _bar("2020-01-03", close=9.5, open_=9, high=10, low=8.5),
        ])
        self.write_cache([["sh.600000", "2020-01-03", "0.7", "1.1", "1.1"]])
        source = self.write_stock_basic()

        panel = self.load()

        np.testing.assert_allclose(panel.back_factor[0], [1, 1.1])
        np.testing.assert_allclose(panel.signal_close[0], [10, 10.45])
        self.assertIn(str(source), {item.path for item in panel.source_hashes})
        self.assertIn(str(self.code_changes), {item.path for item in panel.source_hashes})

    def test_ipo_date_must_equal_first_raw_bar(self):
        # Regressions caught: a stock-basic IPO date is treated as a generic
        # factor-one anchor for a price history that starts after listing.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10)])
        self.write_cache([["sh.600000", "2020-01-03", "0.9", "1", "1"]])
        self.write_stock_basic(ipo_date="2019-12-31")

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "missing_initial_anchor"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_unverified_code_change_blocks_ipo_anchor_without_stock_exception(self):
        # Regressions caught: factor-one listing semantics are applied across
        # a reported code change without source-verified factor-basis continuity.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10)])
        self.write_cache([["sh.600000", "2020-01-04", "0.9", "1", "1"]])
        self.write_stock_basic()
        self.code_changes.write_text(json.dumps({"schema": 1, "events": [
            {"old_code": "600099", "new_code": "600000", "effective_date": "2020-01-03",
             "factor_basis_continuity_verified": False}]}), encoding="utf-8")

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "unverified_code_change"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_unverified_code_change_blocks_even_with_early_factor_event(self):
        # Regressions caught: a historical factor event is mistaken for proof
        # that the same factor basis survives a later code-identity change.
        self.write_bars([
            _bar("2020-01-02", close=10, open_=10, high=10, low=10),
            _bar("2020-01-04", close=11, open_=11, high=11, low=11),
        ])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])
        self.code_changes.write_text(json.dumps({"schema": 1, "events": [
            {"old_code": "600099", "new_code": "600000", "effective_date": "2020-01-03",
             "factor_basis_continuity_verified": False}]}), encoding="utf-8")

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "unverified_code_change"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_malformed_raw_price_is_a_coverage_failure(self):
        # Regressions caught: source-price corruption escapes as an uncaught
        # conversion exception or creates an adjusted signal anyway.
        self.write_bars([_bar("2020-01-02", close=-10, open_=10, high=10, low=10)])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "invalid_lake_price"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_explicit_suspension_keeps_valuation_close_but_not_signal_bar(self):
        # Regressions caught: a stale suspension close gets counted in MA120 or
        # r20, or the raw close needed to value held shares is discarded.
        self.write_bars([
            _bar("2020-01-02", close=10, open_=10, high=11, low=9),
            _bar("2020-01-03", close=10, open_=10, high=10, low=10,
                 volume=0, amount=0, tradestatus=0),
            _bar("2020-01-06", close=11, open_=11, high=12, low=10),
        ])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])

        panel = self.load()

        self.assertEqual(panel.traded_bar[0].tolist(), [True, False, True])
        np.testing.assert_allclose(panel.raw_close[0], [10, 10, 11])
        np.testing.assert_allclose(panel.back_factor[0], [1, 1, 1])
        np.testing.assert_allclose(panel.signal_close[0], [10, np.nan, 11])
        np.testing.assert_allclose(panel.signal_high[0], [11, np.nan, 12])

    def test_trading_status_and_volume_conflict_fails_closed(self):
        # Regressions caught: a provider row says suspended but a positive
        # reported volume is silently treated as either a trade or a pause.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10,
                              volume=1000, amount=0, tradestatus=0)])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "contradictory_trade_state"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_missing_status_and_zero_turnover_is_unknown_not_a_trading_bar(self):
        # Regressions caught: zero O/H/L and a carried close with no status is
        # automatically classified as tradable or quietly skipped.
        self.write_bars([_bar("2020-01-02", close=10, open_=0, high=0, low=0,
                              volume=0, amount=0, tradestatus=None)])
        self.write_cache([["sh.600000", "2020-01-02", "1", "1", "1"]])

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "unknown_trade_state"),
                      {(item.code, item.reason) for item in caught.exception.issues})

    def test_tampered_ipo_snapshot_does_not_establish_anchor(self):
        # Regressions caught: the adapter checks only the file path or schema,
        # not the saved provider response's actual hash.
        self.write_bars([_bar("2020-01-02", close=10, open_=10, high=10, low=10)])
        self.write_cache([["sh.600000", "2020-01-03", "0.9", "1", "1"]])
        path = self.write_stock_basic()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["raw_response"]["rows"][0][2] = "2020-01-01"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(AdjustmentCoverageError) as caught:
            self.load()

        self.assertIn(("600000", "invalid_stock_basic_snapshot"),
                      {(item.code, item.reason) for item in caught.exception.issues})


if __name__ == "__main__":
    unittest.main()
