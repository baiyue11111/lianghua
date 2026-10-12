from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from core.contracts import AccountSnapshot, HistoryBar, MarketFrame, OrderRequest, Side, TargetSet
from core.kernel import Run
from core.interfaces import Components
from plugins.accounts.cash_account import CashAccount
from plugins.markets.cn_equity import AShareExecutionModel, AShareMarketAdapter, AShareRules
from plugins.recorders.jsonl import JsonlRecorder
from plugins.risk.weight_cap import WeightCapRisk
from plugins.strategies.breadth_low_amount_reversal import (
    Strategy,
    Params,
)


START = datetime(2026, 9, 28, 7, 0, tzinfo=UTC)


def fields(open_price: str, close_price: str):
    return {
        f"S{index:02d}": {
            "open": float(open_price),
            "high": float(open_price),
            "low": float(open_price),
            "close": float(close_price),
            "signal_close": 10.0,
            "ma120": 9.0,
            "r20": -0.20 + index / 1000,
            "amount20": 50_000_000 + index * 1_000_000,
            "volume": 10_000_000.0,
            "previous_close": 10.0,
            "tradable": True,
            "limit_locked": False,
        }
        for index in range(80)
    }


def history(at, signal_close):
    return {symbol: tuple(HistoryBar(at-timedelta(days=125-i), {
        "open":10.,"high":10.,"low":10.,"close":10.,"volume":10_000_000.,
        "amount":50_000_000.+rank*1_000_000.,
        "signal_close":9. if i<125 else signal_close+rank/10000,
    }) for i in range(126)) for rank,symbol in enumerate(fields("10","10"))}


def frames():
    result=[]
    for index,(price,signal) in enumerate((("10",10.),("10",10.),("9.9",8.),("9.8",8.))):
        at=START+timedelta(days=index)
        result.append(MarketFrame(at,"CN_EQUITY",fields(price,price),
            {"next_session":(at+timedelta(days=1)).date().isoformat()},history(at,signal)))
    return tuple(result)


def build_rules():
    return AShareRules(
        venue="CN_EQUITY",
        lot_size=100,
        slippage_rate=Decimal("0.001"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
        max_amount_fraction=Decimal("0.01"),
        stamp_duty_schedule=((datetime(1990, 1, 1, tzinfo=UTC), Decimal("0.001")),
                             (datetime(2023, 8, 28, tzinfo=UTC), Decimal("0.0005"))),
        transfer_fee_schedule=((datetime(1990, 1, 1, tzinfo=UTC), Decimal("0")),),
    )


def build_kernel(audit_path: Path):
    rules = build_rules()
    strategy = Strategy(Params(
        positions=8,
        amount_quantile=0.10,
        amount_floor=50_000_000,
        entry_breadth=0.60,
        exit_breadth=0.45,
        breadth_window=1,
        entry_cadence="on_qualification",
    ))
    return Run(Components(
        source=frames(),
        strategy=strategy,
        risk=WeightCapRisk(max_absolute_weight=0.40),
        market=AShareMarketAdapter(rules),
        execution=AShareExecutionModel(rules),
        account=CashAccount(as_of=START - timedelta(days=1), initial_cash=Decimal("100000")),
        recorder=JsonlRecorder(audit_path, run_id="golden-cn-equity"),
    ))


class ClosedLoopCNEquityTests(unittest.TestCase):
    def test_signal_next_open_account_and_exit_form_one_closed_loop(self):
        with TemporaryDirectory() as directory:
            audit = Path(directory) / "events.jsonl"
            kernel = build_kernel(audit)

            results = kernel.run()

            self.assertEqual(len(results[0].planned_orders), 8)
            self.assertEqual({order.side for order in results[0].planned_orders}, {Side.BUY})
            self.assertEqual({order.instrument for order in results[0].planned_orders},
                             {f"S{index:02d}" for index in range(8)})
            self.assertEqual(results[0].execution.fills, ())
            self.assertEqual(len(results[1].execution.fills), 8)
            self.assertEqual(results[1].execution.orders, results[0].planned_orders)
            self.assertEqual(results[1].account.cash, Decimal("3864.000000"))
            self.assertEqual(sum(results[1].account.positions.values()), Decimal("9600"))
            self.assertEqual(len(results[2].planned_orders), 8)
            self.assertEqual({order.side for order in results[2].planned_orders}, {Side.SELL})
            self.assertEqual(len(results[3].execution.fills), 8)
            self.assertEqual(results[3].account.positions, {})
            self.assertEqual(results[3].account.cash, Decimal("97762.927040"))
            self.assertEqual(kernel.pending_orders, ())

            rows = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4])
            self.assertTrue(all(row["run_id"] == "golden-cn-equity" for row in rows))
            self.assertEqual(rows[1]["execution"]["fills"][0]["fee_components"]["commission"], "5")

    def test_same_inputs_produce_identical_audit_bytes(self):
        with TemporaryDirectory() as directory:
            first = Path(directory) / "first.jsonl"
            second = Path(directory) / "second.jsonl"

            build_kernel(first).run()
            build_kernel(second).run()

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_odd_lot_can_be_fully_liquidated(self):
        rules = build_rules()
        at = START + timedelta(days=1)
        market_fields = {
            "ODD": {
                "open": 10.0,
                "volume": 1000.0,
                "amount20": 50_000_000.0,
                "tradable": True,
                "limit_locked": False,
            }
        }
        signal_frame = MarketFrame(START, "CN_EQUITY", market_fields)
        execution_frame = MarketFrame(at, "CN_EQUITY", market_fields)
        account = AccountSnapshot(START, Decimal("0"), Decimal("500"),
                                  {"ODD": Decimal("50")}, {"ODD": Decimal("50")})
        target = TargetSet("test", START, {})
        orders = AShareMarketAdapter(rules).plan(target, signal_frame, account, ())

        execution = AShareExecutionModel(rules).execute(orders, execution_frame, account)

        self.assertEqual(execution.fills[0].quantity, Decimal("50"))

    def test_buy_affordability_includes_every_configured_fee(self):
        rules = replace(
            build_rules(),
            transfer_fee_schedule=((datetime(1990, 1, 1, tzinfo=UTC), Decimal("0.01")),),
        )
        frame = MarketFrame(START + timedelta(days=1), "CN_EQUITY", {
            "ONE": {
                "open": 10.0,
                "volume": 1000.0,
                "amount20": 50_000_000.0,
                "tradable": True,
                "limit_locked": False,
            }
        })
        account = AccountSnapshot(START, Decimal("1006"), Decimal("1006"), {})
        order = OrderRequest("fee-aware", START, "ONE", Side.BUY, Decimal("100"))

        execution = AShareExecutionModel(rules).execute((order,), frame, account)

        self.assertEqual(execution.fills, ())
        self.assertEqual(execution.remaining, (order,))


if __name__ == "__main__":
    unittest.main()
