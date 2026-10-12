"""Independent contract examples, frozen before viewing held-out returns."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from core.contracts import AccountSnapshot, HistoryBar, MarketFrame
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
