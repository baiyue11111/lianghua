from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from threading import Event, Thread
import unittest

import numpy as np

from core.backtest import BacktestError, BacktestRequest
from core.calculation import Calculation
from core.kernel import Kernel
from protocal import DataQuery, Dataset


class FakeSettlement:
    active = None

    def __init__(self, initial_cash, fees, tax_policy, **params):
        self.cash = initial_cash
        self.released = Event()
        self.steps = 0

    def apply(self, record, following, trade_start):
        self.steps += 1
        if FakeSettlement.active is self:
            self.released.wait(2)
        return record.frame, SimpleNamespace()

    def plan(self, target, strategy_id):
        return None

    def fail(self, error):
        return None

    def result(self):
        point = SimpleNamespace(day=date(2020, 1, 1), cash=self.cash, equity=self.cash)
        return SimpleNamespace(equity=(point,), holdings=(), pending=(), pending_orders=(), fills=())


class FakeStrategy:
    strategy_id = "fake"

    def requirements(self):
        return SimpleNamespace(fields=("close",), history=1, price_basis="adjusted",
                               symbols=None, universe="fixture", frequency="daily")

    def generate(self, frame, snapshot):
        return None


class FakeData:
    def __init__(self):
        day = date(2020, 1, 1)
        record = SimpleNamespace(trade_date=day, frame=SimpleNamespace(at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        self.dataset = Dataset("fixture", (record,), {"fees": object(), "tax_policy": object()},
                               {"source": "test"}, {"universe": "fixture", "symbols": ()},
                               (day, date(2020, 1, 2)), ("close",), 1)

    def query(self, query):
        return self.dataset


def request(**changes):
    value = dict(run_id="one", strategy_id="fake", parameters={}, dataset_id="fixture",
                 start=datetime(2020, 1, 1, tzinfo=timezone.utc),
                 end=datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
                 initial_cash=Decimal("100"))
    value.update(changes)
    return BacktestRequest(**value)


class BacktestTests(unittest.TestCase):
    def make_kernel(self, data=None, settlement=FakeSettlement, calculation=None):
        return Kernel(data or FakeData(), lambda _id, _params: FakeStrategy(),
                      lambda req, requirements: DataQuery(
                          req.dataset_id, req.start.date(), req.end.date(),
                          requirements.fields, requirements.history, requirements.price_basis,
                          req.as_of or req.end.date(), requirements.symbols,
                          requirements.universe, requirements.frequency), settlement,
                      calculation=calculation or Calculation)

    def test_query_kernel_settlement_runs_with_fresh_state(self):
        kernel = self.make_kernel()
        first = kernel.run(request(run_id="one"))
        second = kernel.run(request(run_id="two"))
        self.assertEqual(first.final_equity, Decimal("100"))
        self.assertEqual(first.final_equity, second.final_equity)
        self.assertEqual(first.steps, 1)

    def test_kernel_uses_injected_calculation_for_metrics(self):
        class CalculationProbe(Calculation):
            def __init__(self):
                self.calls = []

            def evaluate(self, points, **parameters):
                self.calls.append((tuple(points), parameters))
                return {"probe": 1}

        calculation = CalculationProbe()
        result = self.make_kernel(calculation=calculation).run(request())
        self.assertEqual(result.metrics, {"probe": 1})
        with self.assertRaises(TypeError):
            result.metrics["other"] = 2
        self.assertEqual(len(calculation.calls), 1)
        self.assertEqual(calculation.calls[0][1], {})

    def test_kernel_rejects_invalid_injected_metrics_with_partial_result(self):
        for metrics in ({"bad": float("nan")}, {1: 1.0}, {"bad": True}):
            class CalculationProbe(Calculation):
                def evaluate(self, points, **parameters):
                    return metrics

            with self.subTest(metrics=metrics):
                with self.assertRaises(BacktestError) as caught:
                    self.make_kernel(calculation=CalculationProbe()).run(request())
                self.assertEqual(caught.exception.code, "run_failed")
                self.assertIsNotNone(getattr(caught.exception, "partial_result", None))

    def test_kernel_rejects_non_json_native_metric_numbers(self):
        for metrics in ({"fraction": Fraction(1, 2)}, {"numpy": np.float32(0.5)}):
            class CalculationProbe(Calculation):
                def evaluate(self, points, **parameters):
                    return metrics

            with self.subTest(metrics=metrics):
                with self.assertRaises(BacktestError) as caught:
                    self.make_kernel(calculation=CalculationProbe()).run(request())
                self.assertEqual(caught.exception.code, "run_failed")
                self.assertIsNotNone(getattr(caught.exception, "partial_result", None))

    def test_resident_kernel_rejects_concurrent_request(self):
        entered = Event()

        class Slow(FakeSettlement):
            def apply(self, record, following, trade_start):
                FakeSettlement.active = self
                entered.set()
                return super().apply(record, following, trade_start)

        kernel = self.make_kernel(settlement=Slow)
        result = []
        thread = Thread(target=lambda: result.append(kernel.run(request(run_id="slow"))))
        thread.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(BacktestError) as caught:
            kernel.run(request(run_id="busy"))
        self.assertEqual(caught.exception.code, "busy")
        FakeSettlement.active.released.set()
        thread.join(2)
        FakeSettlement.active = None
        self.assertEqual(len(result), 1)

    def test_request_preserves_declared_symbol_order_and_rejects_bool_history(self):
        value = request(symbols=("B", "A"))
        self.assertEqual(value.symbols, ("B", "A"))
        with self.assertRaises(BacktestError) as caught:
            request(history=True)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_core_imports_without_outer_runtime_modules(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-c", """
import sys
class BlockOuter:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'frontend', 'plugins', 'protocal', 'application', 'history_data'}:
            raise ImportError('outer import')
sys.meta_path.insert(0, BlockOuter())
from core.kernel import Kernel
from core.interfaces import Components
"""], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
