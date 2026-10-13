"""Independent contract examples, frozen before viewing held-out returns."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from protocal import AccountSnapshot, HistoryBar, MarketFrame
from strategy import catalog, load_strategy

MODULE='plugins.strategies.moving_average_cross'


class MovingAverageTests(unittest.TestCase):
    def strategy(self, params):
        self.assertIn(MODULE,[row['module'] for row in catalog()], 'new file must be usable through the existing port')
        return load_strategy(MODULE,params)

    def frame(self, prices, *, trading=True):
        beginning=datetime(2023,1,1,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        rows={symbol:tuple(HistoryBar(beginning+timedelta(days=i),{'signal_close':value})
                           for i,value in enumerate(values)) for symbol,values in prices.items()}
        at=max(row.at for values in rows.values() for row in values)
        return MarketFrame(at,'CN_EQUITY',{}, {'trading':trading},rows)

    def generate(self,strategy,prices,**kwargs):
        frame=self.frame(prices,**kwargs)
        return strategy.generate(frame,AccountSnapshot(frame.at,Decimal(1000),Decimal(1000),{}))

    def test_hand_calculated_entry_hold_exit_and_reentry(self):
        strategy=self.strategy({'fast_window':2,'slow_window':3})
        values=[1,2,3,2,1,2,3]
        actual=[]
        for i in range(1,8):
            target=self.generate(strategy,{'x':values[:i]})
            actual.append(None if target is None else dict(target.weights))
        self.assertEqual(actual,[None,None,{'x':1.0},None,{},None,{'x':1.0}])

    def test_selected_symbols_share_declared_exposure(self):
        strategy=self.strategy({'fast_window':2,'slow_window':3,'exposure':.8})
        target=self.generate(strategy,{'x':[1,2,3],'y':[3,2,1],'z':[2,3,4]})
        self.assertEqual(dict(target.weights),{'x':.4,'z':.4})

    def test_warmup_does_not_consume_first_active_target(self):
        strategy=self.strategy({'fast_window':2,'slow_window':3})
        self.assertIsNone(self.generate(strategy,{'x':[1,2,3]},trading=False))
        target=self.generate(strategy,{'x':[1,2,3,4]})
        self.assertEqual(dict(target.weights),{'x':1.0})

    def test_parameters_change_required_history_and_actual_selection(self):
        fast=self.strategy({'fast_window':1,'slow_window':3})
        slow=self.strategy({'fast_window':2,'slow_window':3})
        self.assertEqual(fast.requirements().history_bars,3)
        self.assertEqual(self.strategy({'fast_window':2,'slow_window':4}).requirements().history_bars,4)
        self.assertEqual(dict(self.generate(fast,{'x':[1,5,2]}).weights),{})
        self.assertEqual(dict(self.generate(slow,{'x':[1,5,2]}).weights),{'x':1.0})
        with self.assertRaises(ValueError):self.strategy({'fast_window':3,'slow_window':3})

    def test_public_parameter_contract_rejects_unknown_and_coerced_values(self):
        with self.assertRaises(ValueError): self.strategy({'fast_window':'2'})
        with self.assertRaises(ValueError): self.strategy({'fast_window':True})
        with self.assertRaises(ValueError): self.strategy({'unknown':2})

    def test_requirements_and_identity_are_declared_by_the_plugin(self):
        strategy=self.strategy({'fast_window':2,'slow_window':5})
        request=strategy.requirements()
        self.assertEqual(strategy.strategy_id,'moving_average_cross')
        self.assertEqual(request.fields,('signal_close',))
        self.assertEqual(request.history_bars,5)
        self.assertEqual(request.frequency,'daily')
        self.assertEqual(request.price_basis,'adjusted')

    def test_empty_target_is_distinct_from_no_decision(self):
        strategy=self.strategy({'fast_window':2,'slow_window':3})
        self.assertIsNone(self.generate(strategy,{'x':[1,2]}))
        entry=self.generate(strategy,{'x':[1,2,3]})
        self.assertEqual(dict(entry.weights),{'x':1.0})
        self.assertIsNone(self.generate(strategy,{'x':[1,2,3]}))
        exit_target=self.generate(strategy,{'x':[3,2,1]})
        self.assertIsNotNone(exit_target)
        self.assertEqual(dict(exit_target.weights),{})

    def test_exposure_changes_weights_without_removing_reason(self):
        strategy=self.strategy({'fast_window':2,'slow_window':3,'exposure':.8})
        target=self.generate(strategy,{'x':[1,2,3],'y':[3,2,1],'z':[2,3,4]})
        self.assertEqual(dict(target.weights),{'x':.4,'z':.4})
        self.assertEqual(dict(target.reasons),{
            'x':'fast_mean_above_slow_mean','z':'fast_mean_above_slow_mean'})

    def test_raw_history_window_controls_activation(self):
        strategy=self.strategy({'fast_window':2,'slow_window':4})
        self.assertIsNone(self.generate(strategy,{'x':[1,2,3]}))
        target=self.generate(strategy,{'x':[1,2,3,4]})
        self.assertEqual(dict(target.weights),{'x':1.0})
