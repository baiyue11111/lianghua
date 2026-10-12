from tests.fee_fixture import fees
from datetime import date
from decimal import Decimal as D
from dataclasses import replace
import unittest

import distribution_ledger
from account_ledger import AccountLedger
from account_execution import AccountExecutionSession
from contracts import Bar, Order, Side
from corporate_actions import calculate_entitlement, normalize_dividend_records


class AccountDistributionTests(unittest.TestCase):
    def setup_claim(self):
        self.assertTrue(hasattr(distribution_ledger,'AccountDistributionLedger'),
                        'distribution settlement must use authoritative account')
        account=AccountLedger(D('10000')); session=AccountExecutionSession(account,fees())
        session.submit(Order('buy','A',Side.BUY,100,date(2020,6,29),date(2020,6,30),'entry'))
        session.execute('buy',Bar('A',date(2020,6,30),11.3,12,11,11.3,1000000,11300000),previous_close=11.3)
        account.mark(date(2020,7,1),{'A':D('11.3')},D(0),D(0))
        event=normalize_dividend_records([{'code':'A','dividRegistDate':'2020-07-01',
            'dividOperateDate':'2020-07-02','dividPayDate':'2020-07-03',
            'dividStockMarketDate':'2020-07-06','dividCashPsBeforeTax':'0.3','dividStocksPs':'0.1'}])[0]
        claim=calculate_entitlement(event,date(2020,7,1),100)
        delivery=distribution_ledger.ShareDelivery(account.lots[0].lot_id,date(2020,6,30),
                                                  10,date(2020,7,3),date(2020,7,6))
        return account,session,distribution_ledger.AccountDistributionLedger(account),claim,delivery

    def test_ex_date_claims_and_actual_payment_preserve_gross_equity_in_one_account(self):
        account,_,ledger,claim,delivery=self.setup_claim()
        before=account.mark(date(2020,7,1),{'A':D('11.3')},D(0),D(0))
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.advance(date(2020,7,2))
        self.assertEqual(ledger.receivable_value(date(2020,7,2),{'A':D(10)}),D(130))
        self.assertEqual(account.mark(date(2020,7,2),{'A':D(10)},D(130),D(0)).equity,before.equity)
        cash=account.cash
        ledger.advance(date(2020,7,3))
        self.assertEqual(account.cash,cash+D(30))
        self.assertEqual(sum(l.shares for l in account.lots),110)
        self.assertEqual(ledger.receivable_value(date(2020,7,3),{'A':D(10)}),D(0))
        self.assertEqual(account.mark(date(2020,7,3),{'A':D(10)},D(0),D(0)).equity,before.equity)
        self.assertEqual(account.lots[-1].acquired,date(2020,6,30))
        self.assertEqual(account.lots[-1].tradable_from,date(2020,7,6))
        postings=account.cash_events
        ledger.advance(date(2020,7,3))
        self.assertEqual(account.cash_events,postings)
        self.assertEqual(sum(l.shares for l in account.lots),110)

    def test_sale_does_not_erase_registration_entitlements(self):
        account,session,ledger,claim,delivery=self.setup_claim()
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.advance(date(2020,7,2))
        session.submit(Order('exit','A',Side.SELL,100,date(2020,7,1),date(2020,7,2),'exit'))
        session.execute('exit',Bar('A',date(2020,7,2),10,11,9,10,1000000,10000000),previous_close=11.3)
        self.assertEqual(account.lots,())
        cash=account.cash
        ledger.advance(date(2020,7,3))
        self.assertEqual(account.cash,cash+D(30))
        self.assertEqual(sum(l.shares for l in account.lots),10)

    def test_credited_bonus_is_not_sellable_until_separate_listing_date(self):
        account,session,ledger,claim,delivery=self.setup_claim()
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.advance(date(2020,7,3))
        session.submit(Order('exit','A',Side.SELL,110,date(2020,7,2),date(2020,7,3),'exit'))
        bar=lambda day:Bar('A',day,10,11,9,10,1000000,10000000)
        first=session.execute('exit',bar(date(2020,7,3)),previous_close=10)
        self.assertEqual(first.shares,100)
        self.assertEqual(session.pending[0].requested_shares,10)
        last=session.execute('exit',bar(date(2020,7,6)),previous_close=10)
        self.assertEqual(last.shares,10)
        self.assertEqual(account.lots,())

    def test_missing_claim_marks_and_backdated_settlement_are_not_substituted(self):
        account,_,ledger,claim,delivery=self.setup_claim()
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.advance(date(2020,7,2))
        with self.assertRaises(ValueError): ledger.receivable_value(date(2020,7,2),{})
        with self.assertRaises(ValueError): ledger.receivable_value(date(2020,7,2),{'A':D('NaN')})
        with self.assertRaises(ValueError): ledger.advance(date(2020,7,1))
        account.mark(date(2020,7,6),{'A':D(10)},D(130),D(0))
        cash=account.cash
        with self.assertRaises(ValueError): ledger.advance(date(2020,7,6))
        self.assertEqual(account.cash,cash)
        self.assertEqual(sum(l.shares for l in account.lots),100)

    def test_distinct_plan_identity_and_conflicting_replay(self):
        account,_,ledger,claim,delivery=self.setup_claim()
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.register('plan-1',claim,'a'*64,(delivery,))
        ledger.register('plan-2',claim,'a'*64,(delivery,))
        with self.assertRaises(ValueError): ledger.register('plan-1',claim,'b'*64,(delivery,))
        ledger.advance(date(2020,7,2))
        self.assertEqual(ledger.receivable_value(date(2020,7,2),{'A':D(10)}),D(260))
        ledger.advance(date(2020,7,3))
        self.assertEqual(sum(l.shares for l in account.lots),120)

    def test_unproven_delivery_or_registration_snapshot_is_rejected_without_posting(self):
        for defect in ('identity','source','missing_delivery','quantity','acquired','date','late_snapshot','fractional','cash_terms'):
            with self.subTest(defect=defect):
                account,_,ledger,claim,delivery=self.setup_claim()
                plan='plan'; source='a'*64; deliveries=(delivery,)
                if defect=='identity': plan=''
                if defect=='source': source='unverified'
                if defect=='missing_delivery': deliveries=()
                if defect=='quantity': deliveries=(replace(delivery,shares=11),)
                if defect=='acquired': deliveries=(replace(delivery,acquired=date(2020,7,1)),)
                if defect=='date': deliveries=(replace(delivery,credit_date=date(2020,7,1)),)
                if defect=='late_snapshot': account.mark(date(2020,7,2),{'A':D(10)},D(0),D(0))
                if defect=='fractional': claim=replace(claim,requires_fractional_allocation=True)
                if defect=='cash_terms': claim=replace(claim,gross_cash=D(31))
                with self.assertRaises(ValueError): ledger.register(plan,claim,source,deliveries)
                ledger.advance(date(2020,7,3))
                self.assertEqual(ledger.receivable_value(date(2020,7,3),{}),D(0))
                self.assertEqual(sum(l.shares for l in account.lots),100)
                self.assertEqual(len([p for p in account.cash_events if p.kind=='dividend']),0)
