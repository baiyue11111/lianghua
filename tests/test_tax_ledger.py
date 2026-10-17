from datetime import date
from decimal import Decimal as D
import unittest
from protocal import Fill, Side
from core.calculation import normalize_dividend_records


def fill(identity,side,day,shares,symbol='A'):
    return Fill(identity,symbol,side,date.fromisoformat(day),shares,10.,shares*10.,0.,0.,'filled')

def event(identity,registration,payment,cash='1',stock='0'):
    return normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'code':identity,'registration_date':registration,
        'operation_date':(date.fromisoformat(registration).replace(day=date.fromisoformat(registration).day+1)).isoformat(),
        'payment_date':payment,'stock_market_date':payment,'cash_per_share':cash,'stock_dividend_per_share':stock}])[0]

class DynamicTaxTests(unittest.TestCase):
    def ledger(self):
        from core.calculation import TradeRules, TaxPolicy
        policy=TaxPolicy.from_mapping({'effective':'2015-09-09','policy':'财税2015-101',
            'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period','rounding':'none',
            'bonus_holding_period':'originating_lot','bonus_tax_allocation':'proportional_entitlement'},'a'*64)
        from tests.fee_fixture import fees
        return TaxHarness(TradeRules({
            'price': {'model':'ratio','params':{'rate':'0','tick':'0.01'}},
            'quantity': {'model':'capacity','params':{}},
            'fees': {'model':'dated','params':{'rounding':'none'}},
            'tax': {'model':'fifo','params':{}},
            'check': {'model':'cn_a_share','params':{'tick':'0.01','min_qty':100,'step':100}},
        }, fees(), policy))
    def day(self,ledger,fills):
        ledger.begin(fills[0].trade_date)
        for item in fills: ledger.preview(item)
        ledger.end(tuple(fills))
    def test_fifo_partial_disposal_and_natural_month_boundaries(self):
        ledger=self.ledger()
        self.day(ledger,[fill('old',Side.BUY,'2020-01-31',100)])
        self.day(ledger,[fill('new',Side.BUY,'2020-02-20',100)])
        ledger.register('div',event('A','2020-02-21','2020-02-25'),D(1),D(0),'b'*64)
        self.assertEqual(ledger.liability(date(2020,2,25)),D(40))
        self.day(ledger,[fill('sell',Side.SELL,'2020-03-01',150)])
        # January31 lot is older than one natural month; February20 lot is not.
        payments=ledger.settle(date(2020,3,1))
        self.assertEqual(sum(p.amount for p in payments),D(20))
        self.assertEqual(ledger.liability(date(2020,3,1)),D(10))
    def test_disposal_before_income_keeps_unpaid_tax_without_cash_debit(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        ledger.register('div',event('A','2020-01-10','2020-01-20'),D(1),D(0),'b'*64)
        self.day(ledger,[fill('sell',Side.SELL,'2020-01-13',100)])
        self.assertEqual(ledger.liability(date(2020,1,13)),D(20))
        self.assertEqual(ledger.settle(date(2020,1,13)),())
        self.assertEqual(sum(p.amount for p in ledger.settle(date(2020,1,20))),D(20))
        self.assertEqual(ledger.liability(date(2020,1,20)),D(0))
        self.assertEqual(ledger.settle(date(2020,1,20)),())
    def test_multiple_distributions_and_long_holding_exemption(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        ledger.register('one',event('A','2020-01-10','2020-01-20'),D(1),D(0),'b'*64)
        ledger.register('two',event('A','2020-02-10','2020-02-20',cash='2'),D(1),D(0),'c'*64)
        self.assertEqual(ledger.liability(date(2021,1,2)),D(30))
        self.assertEqual(ledger.liability(date(2021,1,3)),D(0))
        self.day(ledger,[fill('sell',Side.SELL,'2021-01-04',100)])
        self.assertEqual(ledger.settle(date(2021,1,4)),())
    def test_daily_net_disposal_not_every_sell_fill(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        ledger.register('div',event('A','2020-01-10','2020-01-20'),D(1),D(0),'b'*64)
        self.day(ledger,[fill('sell',Side.SELL,'2020-01-21',100),fill('rebuy',Side.BUY,'2020-01-21',100)])
        self.assertEqual(ledger.settle(date(2020,1,21)),())
        self.assertEqual(ledger.liability(date(2020,1,21)),D(20))
    def test_bonus_income_and_credited_shares_have_explicit_policy(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        ledger.register('div',event('A','2020-01-10','2020-01-20',cash='1',stock='.1'),D(1),D(0),'b'*64,
                        credit_date=date(2020,1,20))
        ledger.credit('bonus','A',date(2020,1,2),10,date(2020,1,20))
        self.assertEqual(ledger.liability(date(2020,1,20)),D(22))
        self.day(ledger,[fill('sell',Side.SELL,'2020-01-21',110)])
        self.assertEqual(sum(p.amount for p in ledger.settle(date(2020,1,21))),D(22))
    def test_tax_reserve_updates_after_sell_before_buy_budget(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        ledger.register('div',event('A','2020-01-10','2020-01-20'),D(1),D(0),'b'*64)
        ledger.begin(date(2020,1,21)); ledger.preview(fill('sell',Side.SELL,'2020-01-21',100))
        self.assertEqual(ledger.reserve(date(2020,1,21)),D(20))
    def test_bonus_uses_net_tax_holding_date_after_same_day_rebuy(self):
        ledger=self.ledger(); self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        self.day(ledger,[fill('sell',Side.SELL,'2020-03-02',100),fill('rebuy',Side.BUY,'2020-03-02',100)])
        ledger.register('bonus',event('A','2020-03-03','2020-03-10',cash='0',stock='.1'),D(1),D(0),'b'*64,credit_date=date(2020,3,10))
        ledger.advance(date(2020,3,10))
        self.assertEqual(sum(l.shares for l in ledger.lots),110)
        self.assertEqual({l.acquired for l in ledger.lots},{date(2020,1,2)})
        self.assertAlmostEqual(ledger.liability(date(2020,3,10)),D(1),places=24)

    def test_unsupported_bonus_policy_never_guesses_holding_date(self):
        from tests.fee_fixture import tax_policy
        ledger=self.ledger()
        from dataclasses import replace
        ledger.ledger.policy=replace(tax_policy(),bonus_holding_period=None)
        self.day(ledger,[fill('buy',Side.BUY,'2020-01-02',100)])
        with self.assertRaisesRegex(ValueError,'bonus'):
            ledger.register('div',event('A','2020-01-10','2020-01-20',stock='.1'),D(1),D(0),'b'*64,credit_date=date(2020,1,20))
    def test_shared_holding_rate_has_exact_natural_month_year_boundaries(self):
        rules = self.ledger().rules
        amount = lambda acquired, disposed, long_rate: rules.tax(
            None, phase='rate', acquired=acquired, disposed=disposed, long_rate=long_rate)
        self.assertEqual(amount(date(2020,1,31),date(2020,2,29),D(0)),D('.2'))
        self.assertEqual(amount(date(2020,1,31),date(2020,3,1),D(0)),D('.1'))
        self.assertEqual(amount(date(2020,2,29),date(2021,2,28),D(0)),D('.1'))
        self.assertEqual(amount(date(2020,2,29),date(2021,3,1),D(0)),D(0))
        self.assertEqual(amount(date(2014,1,1),date(2016,1,1),D('.05')),D('.05'))

    def historical(self):
        from core.calculation import TaxPolicy
        ledger = self.ledger()
        ledger.rules.tax_policy = TaxPolicy.from_mapping({
            'effective':'2013-01-01','policy':'财税2012-85+财税2015-101',
            'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period',
            'rounding':'none'}, 'd'*64)
        ledger.ledger.policy = ledger.rules.tax_policy
        return ledger

    def test_historical_withholding_and_supplement_are_separate(self):
        ledger=self.historical()
        self.day(ledger,[fill('buy',Side.BUY,'2014-01-02',100)])
        ledger.register('old',event('A','2014-01-10','2014-01-20'),D(1),D(0),'b'*64)
        self.assertEqual(ledger.liability(date(2014,1,20)),D(20))
        self.assertEqual(sum(p.amount for p in ledger.settle(date(2014,1,20))),D(5))
        self.assertEqual(ledger.liability(date(2014,1,20)),D(15))
        self.day(ledger,[fill('sell',Side.SELL,'2014-01-21',100)])
        self.assertEqual(sum(p.amount for p in ledger.settle(date(2014,1,21))),D(15))
        self.assertEqual(ledger.liability(date(2014,1,21)),D(0))

    def test_old_distribution_keeps_old_tax_after_regime_changes(self):
        ledger=self.historical()
        self.day(ledger,[fill('buy',Side.BUY,'2014-01-02',100)])
        ledger.register('old',event('A','2015-09-08','2015-09-15'),D(1),D(0),'b'*64)
        self.assertEqual(sum(p.amount for p in ledger.settle(date(2015,9,15))),D(5))
        self.assertEqual(ledger.liability(date(2016,1,4)),D(0))
        self.day(ledger,[fill('sell',Side.SELL,'2016-01-04',100)])
        self.assertEqual(ledger.settle(date(2016,1,4)),())

    def test_modern_distribution_has_no_withholding(self):
        ledger=self.historical()
        self.day(ledger,[fill('buy',Side.BUY,'2015-09-01',100)])
        ledger.register('new',event('A','2015-09-09','2015-09-15'),D(1),D(0),'b'*64)
        self.assertEqual(ledger.settle(date(2015,9,15)),())
        self.assertEqual(ledger.liability(date(2015,9,15)),D(20))


class TaxHarness:
    def __init__(self, rules):
        self.rules=rules
        self.ledger=rules._tax_ledger

    def begin(self, day):
        return self.rules.tax(day, phase='begin')

    def preview(self, fill):
        return self.rules.tax(fill.trade_date, (fill,), phase='preview')

    def end(self, fills):
        return self.rules.tax(fills[0].trade_date, fills, phase='end')

    def liability(self, day):
        return self.rules.tax(day, phase='liability')

    def __getattr__(self, name):
        return getattr(self.ledger, name)
