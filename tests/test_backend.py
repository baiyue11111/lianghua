import asyncio
import importlib
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.history_fixture import make_snapshot

ui = importlib.import_module("frontend.ui-protocol")


def send(protocol, request):
    return asyncio.run(protocol.send(request))[0]


EXECUTION = {
    "price": {"model": "ratio", "params": {"rate": "0.001", "tick": "0.01"}},
    "quantity": {"model": "capacity", "params": {}},
    "fees": {"model": "dated", "params": {"rounding": "none"}},
    "tax": {"model": "fifo", "params": {}},
    "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
}


def settlement_payload():
    return {"limits": {"stock_weight": "0.4", "adv_fraction": "0.01"},
            "execution": EXECUTION}


class EmptyPort:
    def handle(self, operation, data):
        return {}


class ResearchStorageTests(unittest.TestCase):
    def test_relational_result_roundtrip_repeat_and_conflict_rollback(self):
        from types import SimpleNamespace
        from backend import Research, save_backtest
        from protocol import DataError
        from storage import DataStore
        request = SimpleNamespace(run_id='one', strategy_id='strategy', dataset_id='prices', parameters={})
        value = dict(id='one', run_id='one', strategy_id='strategy', dataset_id='prices',
                     first_at='2025-01-02T15:00:00+08:00', last_at='2025-01-03T15:00:00+08:00',
                     steps=2, fills=1, final_cash='900.000000000000000001',
                     final_equity='1000.000000000000000001', pending_orders=0,
                     positions={'600000': '10'}, metrics={'total_return': 0.0},
                     effective_config={}, parameters={},
                     equity=[{'at': '2025-01-03', 'equity': 1000.0}],
                     trades=[dict(order_id='order', symbol='600000', trade_date='2025-01-03',
                                  side='buy', status='filled', shares=10, price=10.0,
                                  gross_amount=100.0, commission=0.0, stamp_duty=0.0,
                                  transfer_fee=0.0, reason=None)],
                     trace={'fills': [{'symbol': '600000'}], 'pending': []})
        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            try:
                save_backtest(store, request, value)
                save_backtest(store, request, value)
                research = Research(store)
                self.assertEqual(research.result('one'), value)
                self.assertEqual(len(research.rows('run_trades', 'one')), 1)
                self.assertEqual(research.detail('one')['trades'], value['trades'])
                with self.assertRaises(DataError):
                    save_backtest(store, request, {**value, 'trades': [dict(value['trades'][0], price=11.0)]})
                self.assertEqual(research.result('one'), value)
                store.close()
                store = DataStore(Path(root))
                self.assertEqual(Research(store).result('one'), value)
            finally:
                store.close()


class LegacyArchiveTests(unittest.TestCase):
    """Attempts saved as documents, before runs were recorded through the lake.

    The directory holds `manifest.json`, `metrics.json` and `result.json` per
    attempt. Nothing imports them: the index and the detail are read from the
    documents as they lie, so the tests write the documents and nothing else.
    """

    def place(self, store, identity, *, strategy='plugins.strategies.one', status='succeeded',
              module=None, total_return=0.25, points=5, trades=3):
        folder = Path(store.root) / 'result' / identity
        folder.mkdir(parents=True)
        manifest = {'schema': 1, 'run_id': identity, 'strategy_id': strategy, 'status': status,
                    'dataset_id': 'cn_a', 'config_hash': 'c', 'data_hash': 'd',
                    'execution_hash': 'e', 'parameters': {'positions': 5, 'unused': None},
                    'effective_config': {'module': module or strategy, 'params': {'positions': 5}},
                    'limitations': ['运行结果未经历史资格认定。']}
        metrics = {'total_return': total_return, 'cagr': 0.1, 'max_drawdown': -0.2,
                   'sharpe': 1.0, 'observations': points, 'initial_equity': 1000.0,
                   'final_equity': 1250.0, 'annualized_volatility': 0.2}
        result = {'id': identity, 'run_id': identity, 'strategy_id': strategy,
                  'metrics': metrics, 'parameters': {'positions': 5},
                  'trades': [{'order_id': f'o{index}', 'symbol': '600000', 'trade_date': '2025-01-03',
                              'side': 'buy', 'status': 'filled', 'shares': 10, 'price': 10.0,
                              'gross_amount': 100.0, 'commission': 0.0, 'stamp_duty': 0.0,
                              'transfer_fee': 0.0, 'reason': None} for index in range(trades)],
                  'equity': [{'at': f'2025-01-{index + 1:02d}', 'equity': 1000.0 + index}
                             for index in range(points)]}
        for name, value in (('manifest.json', manifest), ('metrics.json', metrics),
                            ('result.json', result)):
            (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return folder

    def test_attempts_saved_as_documents_join_the_index_in_one_order(self):
        from backend import Research
        from storage import DataStore
        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            try:
                self.place(store, 'b-legacy')
                research = Research(store)
                page = research.list(limit=10)
                self.assertEqual(page['total'], 1)
                header = page['items'][0]
                self.assertEqual(header['id'], 'b-legacy')
                self.assertEqual(header['metrics']['total_return'], 0.25)
                self.assertEqual(header['module'], 'plugins.strategies.one')
                # The parameters are the ones the document records, unchanged:
                # a key it declares with no value is still a key it declared.
                self.assertEqual(header['parameters'], {'positions': 5, 'unused': None})
                self.assertEqual(header['manifest_file'], 'file:result/b-legacy/manifest.json')

                # A directory that gains an attempt is read again, not believed.
                self.place(store, 'a-legacy')
                self.assertEqual([item['id'] for item in research.list(limit=10)['items']],
                                 ['a-legacy', 'b-legacy'])

                # Two pages of one index, not two slices of two sources.
                first = research.list(limit=1, offset=0)['items']
                second = research.list(limit=1, offset=1)['items']
                self.assertEqual([item['id'] for item in first + second],
                                 ['a-legacy', 'b-legacy'])
            finally:
                store.close()

    def test_archive_strategy_ids_publish_one_canonical_identity(self):
        from backend import Research
        from storage import DataStore
        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            try:
                # Three spellings of archived attempts: full module, a bare
                # short name, and an empty module with a full strategy_id.
                self.place(store, 'a-full', strategy='plugins.strategies.quality_trend')
                self.place(store, 'b-bare', strategy='quality_trend', module='quality_trend')
                self.place(store, 'c-nomodule', strategy='plugins.strategies.moving_average_cross',
                           module=None)
                research = Research(store)
                headers = {item['id']: item['strategy_id'] for item in research.list(limit=10)['items']}
                self.assertEqual(headers, {
                    'a-full': 'plugins.strategies.quality_trend',
                    'b-bare': 'plugins.strategies.quality_trend',
                    'c-nomodule': 'plugins.strategies.moving_average_cross',
                })
                # The detail view publishes the same identity as the index.
                detail = research.detail('b-bare')
                self.assertEqual(detail['strategy_id'], 'plugins.strategies.quality_trend')
            finally:
                store.close()

    def test_a_run_saved_as_documents_opens_by_the_name_in_the_address(self):
        from backend import Research
        from storage import DataStore
        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            try:
                self.place(store, 'legacy-run', points=2000, trades=5)
                research = Research(store)
                value = research.detail('legacy-run', offset=1, limit=2)
                self.assertEqual(value['id'], 'legacy-run')
                self.assertEqual(value['manifest']['run_id'], 'legacy-run')
                self.assertEqual([trade['order_id'] for trade in value['trades']], ['o1', 'o2'])
                self.assertEqual(value['trades_total'], 5)
                # Eight hundred points is what a chart shows, and the last
                # observation is always one of them.
                self.assertEqual(value['equity_sampling'], {'total': 2000, 'stride': 3})
                self.assertEqual(value['equity'][-1]['equity'], 2999.0)
                self.assertEqual(len(value['equity']), 668)
                self.assertEqual({item['file'] for item in value['evidence']},
                                 {f'file:result/legacy-run/{name}' for name in
                                  ('manifest.json', 'metrics.json', 'result.json')})
                self.assertTrue(all(len(item['sha256']) == 64 for item in value['evidence']))
                json.dumps(value)  # the transport can encode it as it stands
            finally:
                store.close()

    def test_a_name_that_cannot_be_a_directory_is_not_looked_up(self):
        from backend import Research
        from storage import DataStore
        with TemporaryDirectory() as root:
            store = DataStore(Path(root))
            try:
                self.place(store, 'legacy-run')
                research = Research(store)
                for name in ('../etc', 'a/b', '.hidden', ''):
                    with self.assertRaises((ValueError, KeyError)):
                        research.detail(name)
                with self.assertRaises(KeyError):
                    research.detail('absent')
                self.assertEqual(Research(store).list(limit=5)['total'], 1)
            finally:
                store.close()


class ProtocolTests(unittest.TestCase):
    def test_extra_limits_are_unlimited_and_lot_size_comes_from_execution(self):
        from core.calculation import AllocationLimits
        from protocol import _limits

        limits = _limits({})
        self.assertEqual(limits.lot_size, 100)
        self.assertIsNone(limits.stock_weight)
        self.assertIsNone(limits.adv_fraction)
        self.assertIsNone(AllocationLimits().stock_weight)

    def test_annual_risk_free_rate_is_explicit_and_quantity_sources_cannot_conflict(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.moving_average_cross", "params": {},
            "dataset_id": "fixture", "start": "2020-01-01", "end": "2020-01-02",
            "initial_cash": "100000",
        }
        request = parse_backtest_request({**base, "evaluation": {"risk_free_rate": 0.03}})
        self.assertEqual(request.evaluation_parameters["risk_free_rate"], 0.03)
        from core.calculation import Calculation
        metrics = Calculation.evaluate(
            [(date(2020, 1, 1), 100.0), (date(2020, 1, 2), 101.0)],
            **dict(request.evaluation_parameters),
        )
        self.assertIn("sharpe", metrics)
        with self.assertRaises(BacktestError):
            parse_backtest_request({**base, "evaluation": {"baseline": "0.03"}})
        with self.assertRaises(BacktestError):
            parse_backtest_request({**base, "evaluation": {"risk_free_rate": "3%"}})
        request = parse_backtest_request({**base, "execution": {
            **EXECUTION, "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 200, "step": 1}}
        }})
        self.assertEqual(request.settlement_parameters["limits"].lot_size, 1)
        with self.assertRaises(BacktestError):
            parse_backtest_request({**base, "limits": {"lot_size": 100}})

    def test_huge_risk_free_rate_is_an_invalid_request(self):
        from protocol import BacktestError, parse_backtest_request

        with self.assertRaises(BacktestError) as caught:
            parse_backtest_request({
                "module": "plugins.strategies.moving_average_cross",
                "params": {"fast_window": 1, "slow_window": 2},
                "dataset_id": "fixture", "start": "2020-01-01", "end": "2020-01-02",
                "initial_cash": "100", "evaluation": {"risk_free_rate": 10 ** 400},
            })
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_settlement_describe_uses_json_null_for_unlimited_limits(self):
        from plugins.data import Settlement
        from protocol import parse_backtest_request

        limits = Settlement.describe({})["params"]["limits"]
        self.assertIsNone(limits["stock_weight"])
        self.assertIsNone(limits["adv_fraction"])

    def test_execution_check_is_the_only_quantity_source_for_allocation(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal
        from plugins.data import Settlement
        from protocol import Bar, TargetSet
        from tests.fee_fixture import fees, tax_policy
        from core.calculation import AccountExecutionSession, AccountLedger, AllocationLimits, TradeRules

        execution = {**EXECUTION, "check": {
            "model": "cn_a_share",
            "params": {"tick": "0.01", "min_qty": 200, "step": 1},
        }}
        limits, normalized = Settlement.resolve(
            limits=AllocationLimits(lot_size=100), execution=execution)
        self.assertEqual(limits.lot_size, 1)
        rules = TradeRules(normalized, fees(), tax_policy())
        target = TargetSet(
            "strategy", datetime(2024, 1, 1, tzinfo=timezone.utc),
            {"600000": 0.1}, {"600000": "entry"},
        )
        plan = rules.allocate(
            target, next_session=date(2024, 1, 2), equity=Decimal("1000000"),
            holdings={}, raw_closes={"600000": Decimal("10")},
            amount20={"600000": Decimal("100000000")}, limits=limits,
        )
        self.assertEqual(plan.orders[0].requested_shares, 10000)
        session = AccountExecutionSession(AccountLedger(Decimal("1000000")), rules)
        order = plan.orders[0]
        session.submit(order)
        fill = session.execute(
            order.order_id,
            Bar("600000", date(2024, 1, 2), 10, 10, 10, 10, 1000000, 10000000),
            previous_close=10,
        )
        self.assertEqual(fill.status, "filled")
        self.assertEqual(fill.shares, 10000)

    def test_atomic_request_rejects_fixed_evaluation_windows(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.moving_average_cross", "params": {},
            "dataset_id": "fixture", "start": "2020-01-01T09:00:00+00:00",
            "end": "2020-01-03T09:00:00+00:00", "initial_cash": 1000000,
        }
        from dataclasses import fields
        from protocol import BacktestRequest

        self.assertNotIn("validation_window", {field.name for field in fields(BacktestRequest)})
        self.assertNotIn("out_of_sample_window", {field.name for field in fields(BacktestRequest)})
        for old_fields in (
            {"validation": {"start": "2020-01-04", "end": "2020-01-05"}},
            {"out_of_sample": {"start": "2020-01-04", "end": "2020-01-05"}},
        ):
            with self.subTest(old_fields=old_fields), self.assertRaises(BacktestError):
                parse_backtest_request({**base, **old_fields})

    def test_execution_and_config_hashes_change_with_selected_execution(self):
        from datetime import datetime, timezone
        from decimal import Decimal
        from types import SimpleNamespace
        from protocol import BacktestRequest, build_effective_config, build_execution_hash

        request = BacktestRequest(
            "hash", "strategy", {}, "fixture",
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 2, tzinfo=timezone.utc), Decimal("100000"),
        )
        strategy = SimpleNamespace(params={})
        requirements = SimpleNamespace(fields=("close",), history_bars=1)
        dataset = SimpleNamespace(fields=("close",), history=1,
                                  scope={"market": {"selection": {"scope": "CN_A"}}})
        provenance = {"data_hash": "data", "source_hashes": {"bars": "source"}, "code_hash": "code"}
        first = {"execution": EXECUTION}
        second = {"execution": {**EXECUTION, "price": {
            "model": "ratio", "params": {"rate": "0.002", "tick": "0.01"},
        }}}

        _, first_config_hash = build_effective_config(request, strategy, requirements, dataset, first)
        _, second_config_hash = build_effective_config(request, strategy, requirements, dataset, second)
        self.assertNotEqual(build_execution_hash(provenance, first), build_execution_hash(provenance, second))
        self.assertNotEqual(first_config_hash, second_config_hash)

    def test_execution_validation_is_shared_by_request_and_settlement(self):
        from protocol import BacktestError, parse_backtest_request
        from plugins.data import Settlement

        invalid = {**EXECUTION,
                   "price": {"model": "ratio", "params": {"rate": "1", "tick": "0.01"}}}
        payload = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000", "execution": invalid,
        }
        with self.assertRaises(BacktestError):
            parse_backtest_request(payload)

        with self.assertRaises(ValueError):
            Settlement.describe({"execution": invalid})
        self.assertEqual(
            Settlement.describe({"execution": EXECUTION})["params"]["execution"]["quantity"]["params"], {}
        )

    def test_execution_rules_reject_invalid_bounds_and_mismatched_ticks(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000",
        }
        for execution in (
            {**EXECUTION, "quantity": {"model": "participation", "params": {"participation": "1.1", "capacity_basis": "prior20"}}},
            {**EXECUTION, "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.02"}}},
        ):
            with self.subTest(execution=execution), self.assertRaises(BacktestError):
                parse_backtest_request({**base, "execution": execution})

    def test_standard_request_keeps_strict_execution_actions(self):
        from protocol import parse_backtest_request

        execution = {
            "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.01"}},
            "quantity": {"model": "participation", "params": {"participation": "0.2", "capacity_basis": "prior20"}},
            "fees": {"model": "dated", "params": {"rounding": "half_up"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }
        request = parse_backtest_request({
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000", "execution": execution,
        })

        self.assertEqual(request.settlement_parameters["execution"], execution)

    def test_standard_request_rejects_old_execution_names_and_unknown_keys(self):
        from protocol import BacktestError, parse_backtest_request

        base = {
            "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {}, "dataset_id": "fixture", "start": "2020-01-31",
            "end": "2020-02-04", "initial_cash": "100000",
        }
        old = {**base, "execution": {
            "price": {"model": "ratio", "params": {"ratio": "0.01", "tick": "0.01"}},
            "quantity": {"model": "cash_capacity", "params": {}},
            "fees": {"model": "dated", "params": {}},
            "tax": {"model": "fifo_natural", "params": {}},
            "check": {"model": "explicit", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(old)

        unknown = {**base, "execution": {
            "price": {"model": "ratio", "params": {"rate": "0.01", "tick": "0.01", "extra": 1}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(unknown)

        malformed = {**base, "execution": {
            "price": {"model": "ratio", "params": {"rate": True, "tick": "0.01"}},
            "quantity": {"model": "capacity", "params": {}},
            "fees": {"model": "dated", "params": {"rounding": "none"}},
            "tax": {"model": "fifo", "params": {}},
            "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
        }}
        with self.assertRaises(BacktestError):
            parse_backtest_request(malformed)

    def test_kernel_result_is_saved_once_in_the_shared_data_store(self):
        from market import DataBus
        from backend import KernelBacktestPort
        from storage import DataStore
        from plugins.data import Settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, Settlement)
            result = port.handle("backtest.run", {
                "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                "params": {"positions": 4}, "dataset_id": "fixture",
                "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                **settlement_payload(),
            })
            from backend import Research
            saved = Research(bus).result('one')

        self.assertEqual(result.data["run_id"], "one")
        self.assertEqual(saved["trace"], result.data["trace"])
        self.assertEqual(saved["equity"], result.data["equity"])

    def test_a_long_backtest_is_submitted_as_a_job_and_read_back(self):
        # The transport allows twenty seconds per request; a backtest is minutes.
        # Submitting and polling keeps the work inside the backend instead of in
        # a second process that would fight the same file for its write lock.
        import time
        from market import DataBus
        from backend import KernelBacktestPort, Research
        from storage import DataStore
        from plugins.data import Settlement

        payload = {
            "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {"positions": 4}, "dataset_id": "fixture",
            "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
            **settlement_payload(),
        }
        with TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            bus = DataBus(DataStore(root))
            port = KernelBacktestPort(bus, Settlement)
            self.assertEqual(port.handle("backtest.job", {}).data["status"], "idle")
            started = port.handle("backtest.submit", payload)
            deadline = time.monotonic() + 120
            while port.handle("backtest.job", {}).data["running"]:
                self.assertLess(time.monotonic(), deadline, "the job never finished")
                time.sleep(0.02)
            view = port.handle("backtest.job", {}).data
            by_id = port.handle("backtest.job", {"id": started.data["job_id"]}).data
            saved = Research(bus).result("one")

        self.assertEqual(started.kind, "operation")
        self.assertEqual(view["status"], "succeeded")
        self.assertEqual(view["result"]["run_id"], "one")
        self.assertEqual(by_id["job_id"], started.data["job_id"])
        self.assertEqual(saved["equity"], view["result"]["equity"])

    def test_a_second_backtest_job_is_refused_while_one_is_running(self):
        import threading
        from market import DataBus
        from backend import KernelBacktestPort
        from protocol import BacktestError
        from storage import DataStore
        from plugins.data import Settlement

        payload = {
            "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
            "params": {"positions": 4}, "dataset_id": "fixture",
            "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
            **settlement_payload(),
        }
        with TemporaryDirectory() as root:
            port = KernelBacktestPort(DataBus(DataStore(Path(root))), Settlement)
            release = threading.Event()
            port._value = lambda request, search, progress=None: (release.wait(10), {})[1]
            port.handle("backtest.submit", payload)
            try:
                with self.assertRaises(BacktestError) as caught:
                    port.handle("backtest.submit", payload)
            finally:
                release.set()
        self.assertEqual(caught.exception.code, "busy")

    def test_protocol_routes_backtest_to_the_injected_port(self):
        from market import DataBus
        from backend import KernelBacktestPort, Modules, Protocol
        from storage import DataStore
        from plugins.data import Settlement

        with TemporaryDirectory() as root:
            root = Path(root)
            config = make_snapshot(root)
            bus = DataBus(DataStore(root))
            backend = Protocol(Modules(
                KernelBacktestPort(bus, Settlement), *[EmptyPort() for _ in range(4)]))
            message = send(backend, ui.UiRequest(
                "one", "backtest.run", "urn:lianghua:ui:input:backtest.run:v1", {
                    "run_id": "one", "module": "plugins.strategies.breadth_low_amount_reversal",
                    "params": {"positions": 4}, "dataset_id": "fixture",
                    "start": "2020-01-31", "end": "2020-02-04", "initial_cash": "100000",
                    **settlement_payload(),
                }))

        wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))
        self.assertEqual(message.kind, "resource")
        self.assertEqual(wire["data"]["run_id"], "one")
        self.assertIn("provenance", wire["data"])

    def test_protocol_messages_carry_semantic_presentation(self):
        from backend import Modules, Protocol

        class SearchPort:
            def handle(self, operation, data):
                return {"items": [{"代码": "sh.600000", "名称": "浦发银行", "最新收盘": 10.21}]}

        backend = Protocol(Modules(EmptyPort(), EmptyPort(), EmptyPort(), SearchPort(), EmptyPort()))
        message = send(backend, ui.UiRequest(
            "two", "data.search", "urn:lianghua:ui:input:data.search:v1", {"q": "600000"}))
        wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))
        presentation = wire["presentation"]
        self.assertEqual(presentation["title"], "证券检索")
        self.assertEqual(presentation["role"], "data-search")
        fields = {item["path"]: item for item in presentation["fields"]}
        self.assertEqual(fields["/items/0/代码"]["role"], "security")
        self.assertEqual(fields["/items/0/代码"]["format"], "code")
        self.assertEqual(fields["/items/0/最新收盘"]["format"], "currency")
        self.assertEqual(fields["/items/0/最新收盘"]["unit"], "CNY")
        self.assertEqual(wire["extensions"]["lianghua:operation"], "data.search")
        self.assertEqual(wire["extensions"]["lianghua:resource"], "data-search")

    def test_problem_messages_are_self_describing(self):
        from backend import Modules, Protocol

        backend = Protocol(Modules(*[EmptyPort() for _ in range(5)]))
        message = send(backend, ui.UiRequest("three", "unknown.operation", "urn:test", {}))
        wire = json.loads(json.dumps(message.to_dict(), allow_nan=False))
        self.assertEqual(message.kind, "problem")
        self.assertEqual(wire["presentation"]["role"], "problem")
        self.assertEqual(wire["extensions"]["lianghua:code"], "unknown_operation")
