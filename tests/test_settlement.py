from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
import unittest
from zoneinfo import ZoneInfo

from plugins.data import Adapter, Settlement
from plugins.strategies.breadth_low_amount_reversal import Params, Strategy
from tests.fee_fixture import settlement as make_settlement
from tests.settlement_fixture import SettlementFixture
from core.calculation import normalize_dividend_records
from protocal import HistoryBar, MarketFrame, RegistrationAction


class DataPluginSettlementTests(unittest.TestCase):
    def test_split_adjustment_preserves_share_value_without_cash_dividend_volume_change(self):
        at = datetime(2020, 1, 2, tzinfo=UTC)
        frame = MarketFrame(
            at,
            "CN_EQUITY",
            {"A": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100.0}},
            {"price_basis": "raw"},
        )
        adjusted = Adapter.adjust(
            (frame,), basis="split-adjusted", as_of=date(2020, 1, 3), anchor="back",
            factors={("A", at): {"factor": 2.0, "direction": "raw_to_target", "anchor": "back",
                                  "as_of": date(2020, 1, 3), "effective_at": at, "known_at": at,
                                  "split_only": True}}
        )[0]
        self.assertEqual(adjusted.fields["A"]["close"], 20.0)
        self.assertEqual(adjusted.fields["A"]["volume"], 50.0)
        self.assertEqual(adjusted.fields["A"]["close"] * adjusted.fields["A"]["volume"], 1000.0)
        self.assertEqual(adjusted.context["as_of"], "2020-01-03")
        with self.assertRaises(ValueError):
            Adapter.adjust(
                (frame,), basis="split-adjusted", as_of=date(2020, 1, 3), anchor="back",
                factors={("A", at): {"factor": 2.0, "direction": "raw_to_target", "anchor": "back",
                                      "as_of": date(2020, 1, 3), "effective_at": at, "known_at": at,
                                      "split_only": False}},
            )
        with self.assertRaisesRegex(ValueError, "not implemented"):
            Adapter.adjust((frame,), basis="adjusted", as_of=date(2020, 1, 3), factors={})

    def test_adjustment_requires_each_timestamp_and_rejects_future_evidence(self):
        first = datetime(2020, 1, 2, tzinfo=UTC)
        second = datetime(2020, 1, 3, tzinfo=UTC)
        frames = tuple(
            MarketFrame(at, "CN_EQUITY", {"A": {"close": 10.0, "volume": 100.0}}, {"price_basis": "raw"})
            for at in (first, second)
        )
        evidence = lambda at, known, cutoff: {
            "factor": 2.0, "direction": "raw_to_target", "anchor": "back",
            "as_of": cutoff, "effective_at": at, "known_at": known, "split_only": True,
        }
        with self.assertRaises(ValueError):
            Adapter.adjust(frames, basis="split-adjusted", as_of=date(2020, 1, 4), factors={("A", first): evidence(first, first, second)})
        with self.assertRaisesRegex(ValueError, "future"):
            Adapter.adjust(
                (frames[0],), basis="split-adjusted", as_of=date(2020, 1, 4),
                factors={("A", first): evidence(first, second, date(2020, 1, 4))},
            )

    def test_adjustment_dates_use_the_aware_frame_timezone(self):
        for timezone in (UTC, ZoneInfo("Asia/Shanghai")):
            with self.subTest(timezone=str(timezone)):
                at = datetime(2020, 1, 2, 15, tzinfo=timezone)
                frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 10.0, "volume": 100.0}}, {"price_basis": "raw"})
                evidence = {
                    "factor": 2.0,
                    "direction": "raw_to_target",
                    "anchor": "back",
                    "as_of": date(2020, 1, 3),
                    "effective_at": date(2020, 1, 2),
                    "known_at": datetime(2020, 1, 1, 15, tzinfo=timezone),
                    "split_only": True,
                }
                adjusted = Adapter.adjust(
                    (frame,), basis="split-adjusted", as_of=date(2020, 1, 3),
                    factors={("A", at): evidence},
                )[0]
                self.assertEqual(adjusted.fields["A"]["close"], 20.0)

    def test_validate_checks_history_values_time_and_required_fields(self):
        at = datetime(2020, 1, 3, 15, tzinfo=UTC)
        future = HistoryBar(datetime(2020, 1, 4, tzinfo=UTC), {"close": 1.0})
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}})
        object.__setattr__(frame, "history", {"A": (future,)})
        with self.assertRaises(ValueError):
            Adapter.validate((frame,))
        bad = HistoryBar(datetime(2020, 1, 2, tzinfo=UTC), {"close": float("nan")})
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}}, history={"A": (bad,)})
        with self.assertRaises(ValueError):
            Adapter.validate((frame,))
        missing = HistoryBar(datetime(2020, 1, 2, tzinfo=UTC), {"other": 1.0})
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}}, history={"A": (missing,)})
        with self.assertRaises(ValueError):
            Adapter.validate((frame,), fields=("close",))
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0, "amount": float("nan")}})
        with self.assertRaises(ValueError):
            Adapter.validate((frame,), fields=("close",))
        extra = HistoryBar(datetime(2020, 1, 2, tzinfo=UTC), {"close": 1.0, "amount": float("nan")})
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}}, history={"A": (extra,)})
        with self.assertRaises(ValueError):
            Adapter.validate((frame,), fields=("close",))

    def test_adjust_rejects_missing_raw_label_and_cutoff_future_frames(self):
        at = datetime(2020, 1, 2, 15, tzinfo=UTC)
        frame = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0, "volume": 1.0}})
        factor = {("A", at): {"factor": 2.0, "direction": "raw_to_target", "anchor": "back",
                               "as_of": date(2020, 1, 3), "effective_at": at, "known_at": at,
                               "split_only": True}}
        with self.assertRaises(ValueError):
            Adapter.adjust((frame,), basis="split-adjusted", as_of=date(2020, 1, 3), factors=factor)
        adjusted = replace(frame, context={"price_basis": "split-adjusted"})
        with self.assertRaises(ValueError):
            Adapter.adjust((adjusted,), basis="split-adjusted", as_of=date(2020, 1, 3), factors=factor)
        with self.assertRaises(ValueError):
            Adapter.adjust((frame,), basis="split-adjusted", as_of=date(2020, 1, 2), factors=factor)

    def test_frozen_history_ab_matches_metrics_equity_and_trace_hashes(self):
        import json
        import tempfile
        from dataclasses import asdict
        from pathlib import Path
        from strategy import canonical_hash, load_strategy
        from tests.history_fixture import make_snapshot
        from tests.test_history_data import kernel_result
        frozen = json.loads(Path("tests/fixtures/kernel-history-ab.json").read_text())
        request = {"module": "plugins.strategies.breadth_low_amount_reversal", "params": {"positions": 4},
                   "initial_cash": 100000, "dataset_id": "fixture", "start": "2020-01-31", "end": "2020-02-04"}
        strategy = load_strategy(request["module"], request["params"])
        request = {**request, "params": asdict(strategy.params)}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = kernel_result(root, make_snapshot(root), {**request, "run_id": "frozen-ab"}, strategy,
                                   loader=lambda _name, _params: strategy)
        hashes = {key: canonical_hash(result["trace"][key]) for key in frozen["trace_hashes"]}
        self.assertEqual(result["equity"], frozen["equity"])
        self.assertEqual(result["metrics"], frozen["metrics"])
        self.assertEqual(hashes, frozen["trace_hashes"])

    def test_validate_checks_default_fields_and_exact_symbol_order(self):
        at = datetime(2020, 1, 2, tzinfo=UTC)
        invalid = MarketFrame(at, "CN_EQUITY", {"A": {"close": "bad"}})
        with self.assertRaises(ValueError):
            Adapter.validate((invalid,))
        extra = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}, "B": {"close": 1.0}})
        with self.assertRaises(ValueError):
            Adapter.validate((extra,), symbols=("A",))
        missing = MarketFrame(at, "CN_EQUITY", {"A": {"close": 1.0}})
        with self.assertRaises(ValueError):
            Adapter.validate((missing,), symbols=("B", "A"))

    def test_settlement_matches_runner_for_nonempty_dividend_and_share_tax_trace(self):
        case = SettlementFixture()
        records = list(case.inputs(exit_market=False))
        event = normalize_dividend_records([{
            "code": "000001", "dividRegistDate": "2020-02-03", "dividOperateDate": "2020-02-04",
            "dividPayDate": "2020-02-05", "dividStockMarketDate": "2020-02-06",
            "dividCashPsBeforeTax": "0.3", "dividStocksPs": "0.1",
        }])[0]
        action = RegistrationAction(
            "plan", event, "a" * 64, date(2020, 2, 5), D(1), D(0), "b" * 64
        )
        records[1] = replace(records[1], registrations=(action,))
        expected = case.run_settlement(records)

        strategy = Strategy(Params(breadth_window=1))
        settlement = make_settlement(D("100000"))
        for index, record in enumerate(records):
            frame, account = settlement.apply(record, case.calendar[index + 1])
            target = strategy.generate(frame, account)
            settlement.plan(target, strategy.strategy_id)
        actual = settlement.result()

        self.assertEqual(actual, expected)
        self.assertEqual(actual.equity[2].receivables, D("1560"))
        self.assertEqual(actual.equity[3].cash, D("4224"))
        self.assertEqual(sum(l.shares for l in actual.holdings[-1][1]), 9720)
        self.assertAlmostEqual(actual.equity[-1].liabilities, D("96"), places=24)

    def test_settlement_rejects_double_apply_and_missing_plan(self):
        case = SettlementFixture()
        record = case.inputs()[0]
        settlement = make_settlement(D("100000"))
        settlement.apply(record, case.calendar[1])
        with self.assertRaises(ValueError):
            settlement.apply(record, case.calendar[1])
        with self.assertRaises(ValueError):
            make_settlement(D("100000")).plan(None, "strategy")
