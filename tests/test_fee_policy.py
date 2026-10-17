from datetime import date
from decimal import Decimal as D
import unittest
from protocal import Side

class FeePolicyTests(unittest.TestCase):
    def fees(self):
        from core.calculation import FeePolicy
        return FeePolicy.from_mapping({'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
            'stamp_rates':[{'effective':'2020-01-01','rate':'.001'},{'effective':'2023-08-28','rate':'.0005'}],
            'transfer_rates':[{'effective':'2020-01-01','rate':'.00002'},{'effective':'2022-04-29','rate':'.00001'}]},'a'*64)
    def rules(self, fee):
        from core.calculation import TaxPolicy, TradeRules
        tax=TaxPolicy.from_mapping({'effective':'2015-09-09','policy':'财税2015-101',
            'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period','rounding':'none'},'b'*64)
        config={'price': {'model':'ratio','params':{'rate':'0','tick':'0.01'}},
            'quantity': {'model':'capacity','params':{}},
            'fees': {'model':'dated','params':{'rounding':'none'}},
            'tax': {'model':'fifo','params':{}},
            'check': {'model':'cn_a_share','params':{'tick':'0.01','min_qty':100,'step':100}}}
        return TradeRules(config, fee, tax)
    def test_historical_transfer_fee_uses_declared_market_and_basis(self):
        raw={'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
             'stamp_rates':[{'effective':'2008-09-19','rate':'.001'}],
             'transfer_rates':[
                 {'effective':'2012-09-01','rate':'.0003','basis':'shares','market':'sh'},
                 {'effective':'2012-09-01','rate':'.0000255','basis':'amount','market':'sz'},
                 {'effective':'2015-08-01','rate':'.00002'}]}
        from core.calculation import FeePolicy
        rule=self.rules(FeePolicy.from_mapping(raw,'a'*64))
        self.assertEqual(rule.fees(D(10000),Side.BUY,date(2014,1,2),symbol='sh.600000',shares=1000).transfer_fee,D('.3'))
        self.assertEqual(rule.fees(D(10000),Side.BUY,date(2014,1,2),symbol='sz.000001',shares=1000).transfer_fee,D('.255'))
        self.assertEqual(rule.fees(D(10000),Side.BUY,date(2015,8,1),symbol='sh.600000',shares=1000).transfer_fee,D('.2'))
        with self.assertRaises(ValueError):
            rule.fees(D(10000),Side.BUY,date(2014,1,2),symbol='hk.000001',shares=1000)

    def test_historical_fee_versions_and_both_side_transfer(self):
        rules=self.rules(self.fees())
        self.assertEqual(tuple(rules.fees(D(100000),Side.SELL,date(2022,4,28)).__dict__.values()),
                         (D(25),D(100),D(2)))
        self.assertEqual(tuple(rules.fees(D(100000),Side.SELL,date(2023,8,28)).__dict__.values()),
                         (D(25),D(50),D(1)))
        self.assertEqual(tuple(rules.fees(D(100000),Side.BUY,date(2023,8,28)).__dict__.values()),
                         (D(25),D(0),D(1)))
    def test_before_declared_effective_date_has_no_rate_fallback(self):
        with self.assertRaisesRegex(ValueError,'effective'):
            self.rules(self.fees()).fees(D(100000),Side.SELL,date(2019,12,31))
    def test_buy_affordability_includes_transfer_fee(self):
        from core.calculation import TradeRules, TaxPolicy
        from protocal import Order, Bar
        from dataclasses import replace
        fees=replace(self.fees(),transfer_rates=((date(2020,1,1),D('.01'),'amount',None),))
        value=TradeRules({'price': {'model':'ratio','params':{'rate':'0','tick':'0.01'}},
            'quantity': {'model':'capacity','params':{}},
            'fees': {'model':'dated','params':{'rounding':'none'}},
            'tax': {'model':'fifo','params':{}},
            'check': {'model':'cn_a_share','params':{'tick':'0.01','min_qty':100,'step':100}}}, fees,
            TaxPolicy.from_mapping({'effective':'2015-09-09','policy':'财税2015-101',
                'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period','rounding':'none'},'b'*64))
        self.assertEqual(value.quantity(Order('o','A',Side.BUY,100,date(2023,1,1),date(2023,1,2),'entry'),
            Bar('A',date(2023,1,2),10,11,9,10,1000000,10000000),price=D('10'),cash=D('1007'),
            positions={},sellable={},reserve=D('0'),volume20=None),0)
