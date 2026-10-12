from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from core.contracts import AccountSnapshot, MarketFrame, TargetSet
from core.kernel import Kernel

NOW = datetime(2020, 1, 31, tzinfo=UTC)

class StrategyPortTests(unittest.TestCase):
    def test_all_strategy_files_expose_the_same_parameter_port(self):
        from strategy import catalog, load_strategy
        ids = {item['strategy_id'] for item in catalog()}
        self.assertEqual(ids, {
            'breadth_low_amount_reversal', 'moving_average_cross',
            'trend_pullback', 'era_leadership', 'quality_trend',
        })
        for item in catalog():
            strategy = load_strategy(item['module'], item['defaults'])
            self.assertTrue(item['parameters'])
            self.assertGreaterEqual(strategy.requirements().history_bars, 1)

    def test_none_keeps_execution_remainder_without_planning(self):
        from tests.test_core_kernel import make_kernel
        trace=[]; kernel=make_kernel(trace)
        class Hold:
            strategy_id='example'
            def generate(self, frame, account): return None
        kernel.components.strategy=Hold()
        result=kernel.step(MarketFrame(NOW,'CN_EQUITY',{}))
        self.assertIsNone(result.target)
        self.assertEqual(result.planned_orders,())
        self.assertNotIn('market',[v[0] for v in trace])

    def test_loader_rejects_unknown_and_coerced_parameters(self):
        from strategy import load_strategy
        module='plugins.strategies.breadth_low_amount_reversal'
        for params in ({'typo':1},{'positions':'8'},{'positions':True}):
            with self.subTest(params=params),self.assertRaises(ValueError):
                load_strategy(module,params)

    def test_windows_change_data_request_and_runtime_identity(self):
        from strategy import load_strategy
        module='plugins.strategies.breadth_low_amount_reversal'
        a=load_strategy(module,{})
        b=load_strategy(module,{'trend_window':160,'return_window':40})
        self.assertNotEqual(a.requirements(),b.requirements())
        self.assertNotEqual(a.params,b.params)

    def test_empty_target_is_distinct_from_no_decision(self):
        from tests.test_core_kernel import make_kernel
        trace=[]; kernel=make_kernel(trace)
        class Exit:
            strategy_id='example'
            def generate(self, frame, account): return TargetSet('example',frame.at,{})
        kernel.components.strategy=Exit()
        # test Market fixture requires an asset key, so use a market spy instead
        class Market:
            def plan(self,target,frame,account,pending):
                trace.append(('market',dict(target.weights))); return ()
        kernel.components.market=Market()
        class Risk:
            def apply(self,target,account): return target
        kernel.components.risk=Risk()
        result=kernel.step(MarketFrame(NOW,'CN_EQUITY',{}))
        self.assertEqual(result.target.weights,{})
        self.assertIn(('market',{}),trace)

    def test_none_preserves_pending_orders_after_an_unfilled_attempt(self):
        from tests.test_core_kernel import make_kernel
        from core.contracts import ExecutionResult, OrderRequest, Side
        trace=[]; kernel=make_kernel(trace)
        order=OrderRequest('pending',NOW-timedelta(days=1),'asset',Side.BUY,Decimal(100))
        kernel._pending_orders=(order,)
        class Hold:
            strategy_id='example'
            def generate(self,frame,account): return None
        class Unfilled:
            def execute(self,orders,frame,account): return ExecutionResult(frame.at,orders,(),orders)
        kernel.components.strategy=Hold(); kernel.components.execution=Unfilled()
        result=kernel.step(MarketFrame(NOW,'CN_EQUITY',{}))
        self.assertEqual(result.planned_orders,(order,))
        self.assertNotIn('market',[v[0] for v in trace])

    def test_risk_cannot_change_weights_without_a_new_reason(self):
        from tests.test_core_kernel import make_kernel
        trace=[]; kernel=make_kernel(trace)
        class SilentRisk:
            def apply(self,target,account): return TargetSet(target.source_id,target.as_of,{'asset':.1},target.reasons)
        kernel.components.risk=SilentRisk()
        with self.assertRaisesRegex(ValueError,'explicit reason'):
            kernel.step(MarketFrame(NOW,'CN_EQUITY',{}))

    def test_defaults_match_frozen_original_selection_and_cash_slots(self):
        from dataclasses import asdict
        from tests.legacy_breadth import BreadthLowAmountReversalStrategy
        from contracts import Candidate, StrategyFrame
        from core.contracts import HistoryBar
        from strategy import load_strategy
        strategy=load_strategy('plugins.strategies.breadth_low_amount_reversal',{})
        symbols=[f'S{i:03}' for i in range(40)]
        history={symbol:tuple(HistoryBar(NOW-timedelta(days=125-j),{
            'open':10.,'high':10.,'low':10.,'close':10.,'volume':10_000_000.,
            'amount':60_000_000.+i*1_000_000.,'signal_close':8. if j<120 else 9. if j<125 else 10.+i/10000,
        }) for j in range(126)) for i,symbol in enumerate(symbols)}
        frame=MarketFrame(NOW,'CN_EQUITY',{}, {'next_session':'2020-02-03'},history)
        breadth,candidates=strategy.features(frame)
        old_frame=StrategyFrame(NOW.date(),1,tuple(Candidate(symbol,None,None,None,amount,10.,8.,None,r20=ret)
            for symbol,amount,ret in candidates),held_days={},market_on=True,market_breadth_120_smoothed=breadth,is_month_end=True)
        old=BreadthLowAmountReversalStrategy().target(old_frame)
        new=strategy.generate(frame,AccountSnapshot(NOW,Decimal(100000),Decimal(100000),{}))
        self.assertEqual(dict(new.weights),dict(old.target_weights))
        self.assertEqual(len(new.weights),4)
        self.assertEqual(sum(new.weights.values()),.5)

    def test_raw_history_changes_indicator_when_window_changes(self):
        from core.contracts import HistoryBar
        from strategy import load_strategy
        from tests.test_closed_loop_cn_equity import history
        frame=MarketFrame(NOW,'CN_EQUITY',{}, {'next_session':'2020-02-03'},history(NOW,10.))
        # Last twenty closes move; forty-day mean includes the earlier segment.
        rows={symbol:tuple(HistoryBar(row.at,{**row.fields,'signal_close':8. if i<100 else 9.+(i-100)/20}) for i,row in enumerate(values)) for symbol,values in frame.history.items()}
        frame=MarketFrame(NOW,'CN_EQUITY',{},frame.context,rows)
        a=load_strategy('plugins.strategies.breadth_low_amount_reversal',{'return_window':20})
        b=load_strategy('plugins.strategies.breadth_low_amount_reversal',{'return_window':40})
        self.assertNotEqual(a.features(frame)[1][0][2],b.features(frame)[1][0][2])
