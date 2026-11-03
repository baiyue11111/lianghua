"""Quality/value proxy: no invented fundamentals or quarterly EPS annualization."""
from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
from protocol import MarketFrame
from strategy import load_strategy

class BuffettTests(unittest.TestCase):
    def frame(self, rows, day='2026-06-30', following='2026-07-01'):
        return MarketFrame(datetime.fromisoformat(day+'T15:00:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')),
                           'CN_EQUITY', rows, {'trading':True,'next_session':following})

    def row(self, **values):
        return dict(close=20.,book_value=10.,roe=.12,net_margin=.15,debt_ratio=.4,
                    eps=1.,cash_per_share=1.) | values

    def strategy(self, **params):
        return load_strategy('plugins.strategies.buffett', params)

    def test_cheapest_qualified_business_and_no_repeat_trade(self):
        strategy=self.strategy(positions=1)
        frame=self.frame({'A':self.row(), 'B':self.row(close=10.),
                          'C':self.row(close=5.,cash_per_share=-1.)})
        self.assertEqual(dict(strategy.generate(frame,None).weights), {'B':1.})
        self.assertIsNone(strategy.generate(frame,None))

    def test_missing_expensive_or_indebted_business_does_not_qualify(self):
        frame=self.frame({'A':self.row(roe=None), 'B':self.row(close=100.),
                          'C':self.row(debt_ratio=.9),'D':self.row(eps=-1.)})
        self.assertEqual(dict(self.strategy().generate(frame,None).weights), {})

    def test_quarter_boundary_and_exit(self):
        strategy=self.strategy()
        self.assertIsNone(strategy.generate(self.frame({'A':self.row()}, '2026-05-29','2026-06-01'),None))
        self.assertEqual(dict(strategy.generate(self.frame({'A':self.row()}),None).weights), {'A':1.})
        self.assertEqual(dict(strategy.generate(self.frame({'A':self.row(roe=-.1)}),None).weights), {})

    def test_invalid_data_and_parameters_rejected(self):
        for row in (self.row(close=0.),self.row(eps=float('nan'))):
            with self.assertRaises(ValueError): self.strategy().generate(self.frame({'A':row}),None)
        with self.assertRaises(ValueError): self.strategy(positions=0)
        with self.assertRaises(ValueError): self.strategy().generate(self.frame({'A':self.row()},following='2026-06-29'),None)

    def test_requests_raw_close_and_pit_fundamentals(self):
        request=self.strategy().requirements()
        self.assertIn('close',request.fields)
        self.assertIn('book_value',request.fields)
        self.assertEqual(request.history_bars,1)
