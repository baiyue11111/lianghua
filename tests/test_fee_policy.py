from datetime import date
from decimal import Decimal as D
import unittest
from protocal import Side

class FeePolicyTests(unittest.TestCase):
    def fees(self):
        from fees import FeePolicy
        return FeePolicy.from_mapping({'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
            'stamp_rates':[{'effective':'2020-01-01','rate':'.001'},{'effective':'2023-08-28','rate':'.0005'}],
            'transfer_rates':[{'effective':'2020-01-01','rate':'.00002'},{'effective':'2022-04-29','rate':'.00001'}]},'a'*64)
    def test_historical_fee_versions_and_both_side_transfer(self):
        fee=self.fees()
        self.assertEqual(fee.calculate(D(100000),Side.SELL,date(2022,4,28)),(D(25),D(100),D(2)))
        self.assertEqual(fee.calculate(D(100000),Side.SELL,date(2023,8,28)),(D(25),D(50),D(1)))
        self.assertEqual(fee.calculate(D(100000),Side.BUY,date(2023,8,28)),(D(25),D(0),D(1)))
    def test_before_declared_effective_date_has_no_rate_fallback(self):
        with self.assertRaisesRegex(ValueError,'effective'):
            self.fees().calculate(D(100000),Side.SELL,date(2019,12,31))
    def test_buy_affordability_includes_transfer_fee(self):
        from execution import ExecutionEngine, PositionBook
        from protocal import Order, Bar
        from dataclasses import replace
        fees=replace(self.fees(),transfer_rates=((date(2020,1,1),D('.01')),))
        fill=ExecutionEngine(fees).execute(Order('o','A',Side.BUY,100,date(2023,1,1),date(2023,1,2),'entry'),
            Bar('A',date(2023,1,2),10,11,9,10,1000000,10000000),PositionBook(1007))
        self.assertEqual(fill.status,'rejected')
