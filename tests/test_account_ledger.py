from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import unittest

from core.calculation import AccountLedger
from protocal import Order,Fill,Side


class AccountLedgerTests(unittest.TestCase):
    def test_share_credit_is_owned_before_it_becomes_tradable(self):
        ledger=AccountLedger(D('100000')); self.buy(ledger)
        ledger.record_share_credit('action',date(2020,1,6),'600000',50,
                                   date(2020,1,3),'c'*64,tradable_from=date(2020,1,9))
        self.assertEqual(sum(l.shares for l in ledger.lots),150)
        self.assertEqual(ledger.mark(date(2020,1,6),{'600000':D('10')},D(0),D(0)).market_value,D('1500'))
        order=Order('s','600000',Side.SELL,150,date(2020,1,6),date(2020,1,7),'exit')
        ledger.register_order(order)
        early=Fill('s','600000',Side.SELL,date(2020,1,7),150,10.,1500.,5.,1.5,'filled')
        with self.assertRaisesRegex(ValueError,'locked'):
            ledger.record_fill('early',early)
        self.assertEqual(sum(l.shares for l in ledger.lots),150)
        ledger.record_fill('settled',replace(early,trade_date=date(2020,1,9)))
        self.assertEqual(ledger.lots,())

    def buy(self, ledger, event_id='b', day=date(2020,1,3)):
        order=Order(event_id,'600000',Side.BUY,100,date(2020,1,2),day,'signal')
        ledger.register_order(order)
        fill=Fill(event_id,'600000',Side.BUY,day,100,10.,1000.,5.,0.,'filled')
        ledger.record_fill(event_id,fill)
        return fill

    def test_cash_value_and_profit_reconcile_after_partial_sale(self):
        ledger=AccountLedger(D('100000'))
        self.buy(ledger)
        order=Order('s','600000',Side.SELL,50,date(2020,1,3),date(2020,1,6),'exit')
        ledger.register_order(order)
        ledger.record_fill('s',Fill('s','600000',Side.SELL,date(2020,1,6),50,12.,600.,5.,.6,'filled'))
        point=ledger.mark(date(2020,1,6),{'600000':D('12')},D(0),D(0))
        self.assertEqual(point.cash,D('99589.4'))
        self.assertEqual(point.market_value,D('600'))
        self.assertEqual(point.equity,D('100189.4'))
        self.assertEqual(point.realized_pnl,D('91.9'))
        self.assertEqual(point.unrealized_pnl,D('97.5'))
        self.assertEqual(point.equity-ledger.initial_cash,point.realized_pnl+point.unrealized_pnl)

    def test_replay_is_idempotent_and_conflict_is_rejected(self):
        ledger=AccountLedger(D('100000')); fill=self.buy(ledger)
        ledger.record_fill('b',fill)
        self.assertEqual(ledger.cash,D('98995'))
        with self.assertRaises(ValueError): ledger.record_fill('b',replace(fill,shares=99))
        self.assertEqual(ledger.cash,D('98995'))

    def test_unordered_oversell_and_same_day_sale_do_not_mutate_account(self):
        ledger=AccountLedger(D('100000')); fill=self.buy(ledger)
        with self.assertRaises(ValueError): ledger.record_fill('unknown',replace(fill,order_id='unknown'))
        order=Order('s','600000',Side.SELL,100,date(2020,1,2),date(2020,1,3),'exit')
        ledger.register_order(order)
        with self.assertRaises(ValueError): ledger.record_fill('s',replace(fill,order_id='s',side=Side.SELL))
        self.assertEqual(ledger.cash,D('98995'))

    def test_sourced_dividend_tax_and_claims_do_not_disappear(self):
        ledger=AccountLedger(D('100000')); self.buy(ledger)
        ledger.record_cash('d',date(2020,1,6),'dividend',D('30'),'a'*64)
        ledger.record_cash('t',date(2020,1,6),'dividend_tax',D('-6'),'b'*64)
        ledger.record_cash('d',date(2020,1,6),'dividend',D('30'),'a'*64)
        point=ledger.mark(date(2020,1,6),{'600000':D('10')},D('5'),D('2'))
        self.assertEqual(point.cash,D('99019'))
        self.assertEqual(point.equity,D('100022'))
        self.assertEqual(point.net_cash_income,D('24'))
        with self.assertRaises(ValueError): ledger.record_cash('bad',date(2020,1,6),'dividend',D(1),'')
        with self.assertRaises(ValueError): ledger.mark(date(2020,1,6),{},D(0),D(0))

    def test_actual_share_credit_is_not_an_adjustment_factor(self):
        ledger=AccountLedger(D('100000')); self.buy(ledger)
        ledger.record_share_credit('action',date(2020,1,6),'600000',10,date(2020,1,6),'c'*64,tradable_from=date(2020,1,6))
        ledger.record_share_credit('action',date(2020,1,6),'600000',10,date(2020,1,6),'c'*64,tradable_from=date(2020,1,6))
        point=ledger.mark(date(2020,1,6),{'600000':D('9')},D(0),D(0))
        self.assertEqual(point.market_value,D('990'))
        self.assertEqual(point.equity,D('99985'))
        with self.assertRaises(ValueError): ledger.record_share_credit('fraction',date(2020,1,6),'600000',D('1.5'),date(2020,1,6),'c'*64,tradable_from=date(2020,1,6))


if __name__=='__main__': unittest.main()
