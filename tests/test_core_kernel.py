from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from core.contracts import (
    AccountSnapshot,
    ExecutionResult,
    FillEvent,
    MarketFrame,
    OrderRequest,
    Side,
    StepResult,
    TargetSet,
)
from core.kernel import ContractViolation, Run
from core.interfaces import Components


NOW = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)


class Source:
    def __init__(self, frames):
        self.frames = tuple(frames)

    def __iter__(self):
        return iter(self.frames)


class Strategy:
    strategy_id = "example"

    def __init__(self, trace, weight=0.5):
        self.trace = trace
        self.weight = weight

    def generate(self, frame, account):
        self.trace.append(("strategy", frame.venue, account.cash))
        return TargetSet(self.strategy_id, frame.at, {"asset": self.weight}, {"asset": "signal"})


class Risk:
    def __init__(self, trace):
        self.trace = trace

    def apply(self, target, account):
        self.trace.append(("risk", target.weights["asset"], account.equity))
        return target


class Market:
    def __init__(self, trace):
        self.trace = trace

    def plan(self, target, frame, account, pending):
        self.trace.append(("market", target.weights["asset"], frame.venue))
        return (OrderRequest("o1", target.as_of, "asset", Side.BUY, Decimal("0.25")),)


class Execution:
    def __init__(self, trace):
        self.trace = trace

    def execute(self, orders, frame, account):
        self.trace.append(("execution", len(orders), frame.venue))
        fills = tuple(
            FillEvent(order.order_id, frame.at, order.instrument, order.side,
                      order.quantity, Decimal("20"), Decimal("0.01"))
            for order in orders
        )
        return ExecutionResult(frame.at, orders, fills)


class Account:
    def __init__(self, trace):
        self.trace = trace
        self.current = AccountSnapshot(NOW - timedelta(days=1), Decimal("100"), Decimal("100"), {})

    def snapshot(self):
        return self.current

    def apply(self, result, frame):
        self.assert_frame = frame
        self.trace.append(("account", len(result.fills)))
        if result.fills:
            self.current = AccountSnapshot(result.as_of, Decimal("94.99"), Decimal("99.99"), {"asset": Decimal("0.25")})
        else:
            self.current = AccountSnapshot(result.as_of, self.current.cash, self.current.equity, self.current.positions)
        return self.current


class Recorder:
    def __init__(self, trace):
        self.trace = trace
        self.results = []

    def record(self, result):
        self.trace.append(("recorder", result.account.cash, result.account.equity))
        self.results.append(result)


def make_kernel(trace, *, weight=0.5, source=()):
    return Run(Components(
        source=source,
        strategy=Strategy(trace, weight),
        risk=Risk(trace),
        market=Market(trace),
        execution=Execution(trace),
        account=Account(trace),
        recorder=Recorder(trace),
    ))


class KernelTests(unittest.TestCase):
    def test_step_routes_standard_contracts_in_one_direction(self):
        trace = []
        kernel = make_kernel(trace)
        frame = MarketFrame(NOW, "CRYPTO", {"asset": {"close": 20.0}})

        result = kernel.step(frame)

        self.assertIsInstance(result, StepResult)
        self.assertEqual(result.account.cash, Decimal("100"))
        self.assertEqual(len(result.planned_orders), 1)
        self.assertEqual(result.execution.fills, ())
        self.assertEqual(
            [entry[0] for entry in trace],
            ["execution", "account", "strategy", "risk", "market", "recorder"],
        )

    def test_strategy_weight_is_used_without_forced_allocation(self):
        trace = []
        result = make_kernel(trace, weight=0.2).step(
            MarketFrame(NOW, "US_EQUITY", {"asset": {"close": 20.0}})
        )

        self.assertEqual(result.target.weights, {"asset": 0.2})
        self.assertEqual(trace[2], ("strategy", "US_EQUITY", Decimal("100")))
        self.assertEqual(trace[4], ("market", 0.2, "US_EQUITY"))

    def test_contract_violation_stops_instead_of_falling_back(self):
        trace = []
        kernel = make_kernel(trace)
        original = kernel.components.strategy

        class FutureStrategy:
            strategy_id = original.strategy_id

            def generate(self, frame, account):
                return TargetSet(self.strategy_id, frame.at + timedelta(days=1), {}, {})

        kernel = replace(kernel, components=replace(kernel.components, strategy=FutureStrategy()))

        with self.assertRaisesRegex(ContractViolation, "strategy target timestamp"):
            kernel.step(MarketFrame(NOW, "CN_EQUITY", {}))

        self.assertEqual([entry[0] for entry in trace], ["execution", "account"])

    def test_run_replays_source_in_order(self):
        trace = []
        kernel = make_kernel(trace)
        frames = (
            MarketFrame(NOW, "CN_EQUITY", {}),
            MarketFrame(NOW + timedelta(days=1), "CN_EQUITY", {}),
        )

        kernel.components.source = Source(frames)
        results = kernel.run()

        self.assertEqual(tuple(result.frame.at for result in results), tuple(frame.at for frame in frames))
        self.assertEqual(len(kernel.components.recorder.results), 2)
        self.assertEqual(results[0].execution.orders, ())
        self.assertEqual(results[1].execution.orders, results[0].planned_orders)
        self.assertEqual(results[1].account.cash, Decimal("94.99"))
        strategy_entries = [entry for entry in trace if entry[0] == "strategy"]
        self.assertEqual(strategy_entries[1], ("strategy", "CN_EQUITY", Decimal("94.99")))

    def test_contract_copies_nested_input_mappings(self):
        fields = {"asset": {"close": 20.0}}
        frame = MarketFrame(NOW, "CN_EQUITY", fields)

        fields["asset"]["close"] = 99.0

        self.assertEqual(frame.fields["asset"]["close"], 20.0)
        with self.assertRaises(TypeError):
            frame.fields["asset"]["close"] = 21.0

    def test_run_cannot_continue_after_recorder_failure(self):
        trace = []
        run = make_kernel(trace)
        class Broken:
            def record(self, result):
                raise OSError('disk full')
        run.components.recorder = Broken()
        with self.assertRaises(OSError):
            run.step(MarketFrame(NOW, 'CN_EQUITY', {}))
        calls = len(trace)
        with self.assertRaisesRegex(ValueError, 'failed run'):
            run.step(MarketFrame(NOW + timedelta(days=1), 'CN_EQUITY', {}))
        self.assertEqual(len(trace), calls)

    def test_completed_run_cannot_restart_or_accept_another_frame(self):
        from core.backtest import BacktestError
        run = make_kernel([], source=(MarketFrame(NOW, 'CN_EQUITY', {}),))
        run.run()
        with self.assertRaises(BacktestError) as caught:
            run.run()
        self.assertEqual(caught.exception.code, 'run_reused')
        with self.assertRaises(BacktestError) as caught:
            run.step(MarketFrame(NOW+timedelta(days=1), 'CN_EQUITY', {}))
        self.assertEqual(caught.exception.code, 'run_closed')


if __name__ == "__main__":
    unittest.main()
