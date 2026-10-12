from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import ast
import unittest
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from tests.test_closed_loop_cn_equity import START, build_kernel, frames


class Factory:
    def __init__(self, root):
        self.root = Path(root)
        self.closed = 0
        self.runs = []

    @contextmanager
    def open(self, request):
        run = build_kernel(self.root / (request.run_id + '.jsonl')).components
        self.runs.append(run)
        try:
            yield run
        finally:
            self.closed += 1


def request(run_id='one', **changes):
    from core.backtest import BacktestRequest
    return replace(BacktestRequest(
        run_id=run_id, strategy_id='breadth_low_amount_reversal',
        parameters={}, dataset_id='sample', start=START,
        end=START + timedelta(days=3), initial_cash=Decimal('100000'),
    ), **changes)


class BacktestTests(unittest.TestCase):
    def test_resident_backend_isolates_runs_and_preserves_golden_accounting(self):
        from core.kernel import Kernel
        with TemporaryDirectory() as root:
            factory = Factory(root)
            backend = Kernel(factory)
            a = backend.run(request('a'))
            b = backend.run(request('b'))
            self.assertEqual(a.final_cash, Decimal('97762.927040'))
            self.assertEqual(a.final_equity, b.final_equity)
            self.assertEqual(a.steps, 4)
            self.assertEqual(a.fills, 16)
            self.assertEqual(a.pending_orders, 0)
            self.assertEqual(factory.closed, 2)
            self.assertIsNot(factory.runs[0].account, factory.runs[1].account)
            self.assertIsNot(factory.runs[0].strategy, factory.runs[1].strategy)
            self.assertFalse(hasattr(a, 'results'))

    def test_failure_closes_run_and_next_request_starts_clean(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        class BadFirst(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    if request.run_id == 'bad':
                        run.source = (*frames()[:2], object())
                    yield run
        with TemporaryDirectory() as root:
            factory = BadFirst(root)
            backend = Kernel(factory)
            with self.assertRaises(BacktestError) as caught:
                backend.run(request('bad'))
            self.assertEqual(caught.exception.code, 'run_failed')
            self.assertIsNotNone(caught.exception.__cause__)
            self.assertEqual(factory.closed, 1)
            self.assertEqual(backend.run(request('good')).final_cash, Decimal('97762.927040'))

    def test_reused_run_is_rejected(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        with TemporaryDirectory() as root:
            run = build_kernel(Path(root) / 'same.jsonl').components
            class Reused:
                @contextmanager
                def open(self, request):
                    yield run
            backend = Kernel(Reused())
            backend.run(request())
            with self.assertRaises(BacktestError) as caught:
                backend.run(request('two'))
            self.assertEqual(caught.exception.code, 'run_reused')

    def test_empty_wrong_strategy_and_wrong_cash_fail_before_success(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        for changes, code in (({'strategy_id': 'other'}, 'strategy_mismatch'),
                              ({'initial_cash': Decimal('200000')}, 'account_mismatch')):
            with self.subTest(code=code), TemporaryDirectory() as root:
                with self.assertRaises(BacktestError) as caught:
                    Kernel(Factory(root)).run(request(**changes))
                self.assertEqual(caught.exception.code, code)
        class Empty(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    run.source = ()
                    yield run
        with TemporaryDirectory() as root:
            with self.assertRaises(BacktestError) as caught:
                Kernel(Empty(root)).run(request())
            self.assertEqual(caught.exception.code, 'empty_source')

    def test_invalid_request_and_mutable_parameters(self):
        from core.backtest import BacktestError
        params = {'window': 5}
        value = request(parameters=params)
        params['window'] = 10
        self.assertEqual(value.parameters['window'], 5)
        for changes in ({'initial_cash': Decimal('NaN')}, {'initial_cash': 100},
                        {'start': START.replace(tzinfo=None)}, {'end': START-timedelta(days=1)},
                        {'run_id': ''}, {'parameters': {'x': []}},
                        {'parameters': {'x': float('inf')}}):
            with self.subTest(changes=changes), self.assertRaises(BacktestError) as caught:
                request(**changes)
            self.assertEqual(caught.exception.code, 'invalid_request')

    def test_dependency_direction_is_enforced_for_core_and_protocol(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        for path in (*sorted((root/'core').glob('*.py')), root/'protocal.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                         else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    blocked = {'frontend', 'plugins', 'history_data'}
                    if path.parent.name == 'core':
                        blocked.add('application')
                        self.assertNotEqual(name.split('.')[0], 'protocal')
                    self.assertNotIn(name.split('.')[0], blocked)

    def test_core_imports_without_loading_frontend_or_plugins(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
class BlockOuter:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'frontend', 'plugins', 'protocal', 'application', 'history_data'}:
            raise ImportError('core imported an outer layer: ' + fullname)
sys.meta_path.insert(0, BlockOuter())
from core.kernel import Kernel
from core.interfaces import Components, RunFactory
from core.backtest import BacktestRequest
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_contract_import_does_not_initialize_the_kernel(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
from core.contracts import MarketFrame
assert 'core.kernel' not in sys.modules, 'entity import eagerly loaded the application kernel'
assert 'core.interfaces' not in sys.modules, 'entity import eagerly loaded component ports'
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parameter_and_strategy_replacement_use_the_same_boundary(self):
        from core.kernel import Kernel
        from strategy import load_strategy
        class CrossFactory(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    run.strategy = load_strategy('plugins.strategies.' + request.strategy_id,
                                                 dict(request.parameters))
                    run.source = tuple(replace(frame, context={**frame.context, 'trading': True})
                                       for frame in frames())
                    yield run
        with TemporaryDirectory() as root:
            backend = Kernel(CrossFactory(root))
            a = backend.run(request('off', strategy_id='moving_average_cross',
                                    parameters={'fast_window': 1, 'slow_window': 2, 'exposure': 0.}))
            b = backend.run(request('on', strategy_id='moving_average_cross',
                                    parameters={'fast_window': 1, 'slow_window': 2, 'exposure': 1.}))
            self.assertEqual(a.fills, 0)
            self.assertEqual(a.final_cash, Decimal('100000'))
            self.assertGreater(b.fills, 0)
            self.assertNotEqual(a.final_equity, b.final_equity)

    def test_duplicate_and_outside_frames_are_rejected_before_execution(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        for data, code, count in ((frames()[:1]*2, 'run_failed', 1),
                                 ((replace(frames()[0], at=START-timedelta(days=1), history={}),), 'frame_out_of_range', 0)):
            class BadSource(Factory):
                @contextmanager
                def open(self, request):
                    with super().open(request) as run:
                        run.source = data
                        yield run
            with self.subTest(code=code), TemporaryDirectory() as root:
                with self.assertRaises(BacktestError) as caught:
                    Kernel(BadSource(root)).run(request())
                self.assertEqual(caught.exception.code, code)
                path = Path(root)/'one.jsonl'
                self.assertEqual(len(path.read_text().splitlines()) if path.exists() else 0, count)

    def test_concurrent_request_is_rejected_without_opening_another_run(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        entered, release = Event(), Event()
        class Slow(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test release not signaled')
                    yield run
        with TemporaryDirectory() as root, ThreadPoolExecutor(max_workers=1) as pool:
            factory = Slow(root)
            backend = Kernel(factory)
            future = pool.submit(backend.run, request('first'))
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(BacktestError) as caught:
                    backend.run(request('second'))
                self.assertEqual(caught.exception.code, 'busy')
                self.assertEqual(len(factory.runs), 1)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).steps, 4)
            self.assertEqual(backend.run(request('third')).steps, 4)

    def test_factory_cannot_supply_an_execution_engine(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        with TemporaryDirectory() as root:
            class EngineFactory:
                @contextmanager
                def open(self, request):
                    yield build_kernel(Path(root)/'invalid.jsonl')
            with self.assertRaises(BacktestError) as caught:
                Kernel(EngineFactory()).run(request())
            self.assertEqual(caught.exception.code, 'run_failed')
            self.assertFalse((Path(root)/'invalid.jsonl').exists())

    def test_factory_cleanup_failure_does_not_return_success(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        class BadClose(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    yield run
                    raise OSError('cannot flush audit')
        with TemporaryDirectory() as root:
            with self.assertRaises(BacktestError) as caught:
                Kernel(BadClose(root)).run(request())
            self.assertEqual(caught.exception.code, 'run_failed')

    def test_factory_cannot_hide_source_failure(self):
        from core.backtest import BacktestError
        from core.kernel import Kernel
        class Suppressed(Factory):
            @contextmanager
            def open(self, request):
                with super().open(request) as run:
                    run.source = (object(),)
                    try:
                        yield run
                    except TypeError:
                        pass
        with TemporaryDirectory() as root:
            with self.assertRaises(BacktestError):
                Kernel(Suppressed(root)).run(request())
