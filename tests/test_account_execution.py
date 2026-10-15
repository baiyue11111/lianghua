from tests.fee_fixture import rules
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import unittest

from core.calculation import AccountLedger
from protocal import Bar, Fill, Order, Side
from core.calculation import AccountExecutionSession
from core.calculation import OpeningRiskSnapshot


class AccountExecutionTests(unittest.TestCase):
    def bar(self, day, price=10):
        return Bar('600000',day,price,price+1,price-1,price,100000,1000000)

    def test_open_batch_sells_before_buying_with_released_cash(self):
        account=AccountLedger(D('1007')); session=AccountExecutionSession(account,rules())
        session.submit(Order('seed','600000',Side.BUY,100,date(2020,1,2),date(2020,1,3),'entry'))
        session.execute('seed',self.bar(date(2020,1,3)),previous_close=10)
        self.assertEqual(account.cash,D('1'))
        session.submit(Order('a-buy','600001',Side.BUY,100,date(2020,1,3),date(2020,1,6),'replace'))
        session.submit(Order('z-sell','600000',Side.SELL,100,date(2020,1,3),date(2020,1,6),'replace'))
        day=date(2020,1,6)
        self.assertTrue(hasattr(session,'execute_open'),'missing daily account execution boundary')
        fills=session.execute_open(day,{'600001':replace(self.bar(day,4),symbol='600001'),
                                        '600000':self.bar(day,12)},
                                   {'600000':10,'600001':4},
                                   risk=OpeningRiskSnapshot(date(2020,1,3),
                                       {'600000':D(12),'600001':D(4)},
                                       {'600000':D('50000000'),'600001':D('50000000')},D(0),D(0)))
        self.assertEqual([f.order_id for f in fills],['z-sell','a-buy'])
        self.assertEqual([f.status for f in fills],['filled','filled'])
        self.assertEqual([f.price for f in fills],[11.98,4.01])
        # 1 + (100*11.98 - 5 - 1.198) - (100*4.01 + 5)
        self.assertEqual(account.cash,D('786.802'))
        self.assertEqual([(l.symbol,l.shares) for l in account.lots],[('600001',100)])

    def test_open_batch_validates_all_inputs_before_any_posting(self):
        for defect in ('missing_bar','wrong_day','wrong_symbol','missing_previous'):
            with self.subTest(defect=defect):
                account=AccountLedger(D('100000')); session=AccountExecutionSession(account,rules())
                day=date(2020,1,3)
                for symbol in ('600000','600001'):
                    session.submit(Order(symbol,symbol,Side.BUY,100,date(2020,1,2),day,'entry'))
                bars={s:replace(self.bar(day),symbol=s) for s in ('600000','600001')}
                previous={'600000':10,'600001':10}
                if defect=='missing_bar': del bars['600001']
                if defect=='wrong_day': bars['600001']=replace(bars['600001'],trade_date=date(2020,1,6))
                if defect=='wrong_symbol': bars['600001']=self.bar(day)
                if defect=='missing_previous': del previous['600001']
                self.assertTrue(hasattr(session,'execute_open'),'missing daily account execution boundary')
                risk=OpeningRiskSnapshot(date(2020,1,2),{'600000':D(10),'600001':D(10)},
                                         {'600000':D('50000000'),'600001':D('50000000')},D(0),D(0))
                with self.assertRaises(ValueError): session.execute_open(day,bars,previous,risk=risk)
                self.assertEqual(account.cash,D('100000'))
                self.assertEqual(account.lots,())
                self.assertEqual(session.fills,())
                self.assertEqual(len(session.pending),2)

    def test_fills_post_to_one_account_and_replay_does_not_post_twice(self):
        account=AccountLedger(D('100000')); session=AccountExecutionSession(account,rules())
        order=Order('buy','600000',Side.BUY,200,date(2020,1,2),date(2020,1,3),'entry')
        session.submit(order)
        bar=self.bar(date(2020,1,3))
        fill=session.execute('buy',bar,previous_close=10)
        self.assertEqual(fill.shares,200)
        self.assertEqual(account.cash,D('97993'))
        self.assertEqual(sum(l.shares for l in account.lots),200)
        self.assertEqual(session.pending,())
        count=len(account.cash_events)
        self.assertEqual(session.execute('buy',bar,previous_close=10),fill)
        self.assertEqual(len(account.cash_events),count)
        session.submit(order)
        self.assertEqual(session.pending,())
        with self.assertRaises(ValueError): session.execute('buy',replace(bar,open=11),previous_close=10)
        with self.assertRaises(ValueError): session.submit(replace(order,requested_shares=300))
        sell=Order('sell','600000',Side.SELL,100,date(2020,1,3),date(2020,1,6),'exit')
        session.submit(sell)
        sell_fill=session.execute('sell',self.bar(date(2020,1,6),12),previous_close=10)
        self.assertEqual(sell_fill.price,11.98)
        point=account.mark(date(2020,1,6),{'600000':D('12')},D(0),D(0))
        self.assertEqual(point.cash,D('99184.802'))
        self.assertEqual(point.market_value,D('1200'))
        self.assertEqual(point.equity,D('100384.802'))
        self.assertEqual(point.realized_pnl,D('188.302'))
        self.assertEqual(point.unrealized_pnl,D('196.5'))

    def test_credited_but_locked_shares_remain_pending_until_tradable(self):
        account=AccountLedger(D('100000')); session=AccountExecutionSession(account,rules())
        session.submit(Order('buy','600000',Side.BUY,100,date(2020,1,2),date(2020,1,3),'entry'))
        session.execute('buy',self.bar(date(2020,1,3)),previous_close=10)
        account.record_share_credit('bonus',date(2020,1,6),'600000',50,date(2020,1,3),
                                    'a'*64,tradable_from=date(2020,1,8))
        order=Order('sell','600000',Side.SELL,150,date(2020,1,3),date(2020,1,6),'exit')
        session.submit(order)
        first=session.execute('sell',self.bar(date(2020,1,6)),previous_close=10)
        self.assertEqual(first.shares,100)
        self.assertEqual(session.pending[0].requested_shares,50)
        self.assertEqual(session.pending[0].signal_date,order.signal_date)
        self.assertEqual(sum(l.shares for l in account.lots),50)
        cash=account.cash
        rejected=session.execute('sell',self.bar(date(2020,1,7)),previous_close=10)
        self.assertEqual(rejected.status,'rejected')
        self.assertEqual(account.cash,cash)
        final=session.execute('sell',self.bar(date(2020,1,8)),previous_close=10)
        self.assertEqual(final.shares,50)
        self.assertEqual(account.lots,())
        self.assertEqual(session.pending,())

    def test_new_signal_cancellation_blocks_remaining_fills_and_is_audited(self):
        account=AccountLedger(D('100000')); session=AccountExecutionSession(account,rules())
        session.submit(Order('buy','600000',Side.BUY,100,date(2020,1,2),date(2020,1,3),'entry'))
        session.execute('buy',self.bar(date(2020,1,3)),previous_close=10)
        account.record_share_credit('bonus',date(2020,1,6),'600000',50,date(2020,1,3),
                                    'a'*64,tradable_from=date(2020,1,8))
        session.submit(Order('sell','600000',Side.SELL,150,date(2020,1,3),date(2020,1,6),'exit'))
        session.execute('sell',self.bar(date(2020,1,6)),previous_close=10)
        session.cancel('sell',date(2020,1,7),'new-monthly-target')
        session.cancel('sell',date(2020,1,7),'new-monthly-target')
        self.assertEqual(session.pending,())
        self.assertEqual(len(account.order_cancellations),1)
        self.assertEqual(account.order_cancellations[0].reason,'new-monthly-target')
        with self.assertRaises(ValueError): session.cancel('sell',date(2020,1,7),'different')
        with self.assertRaises(KeyError): session.execute('sell',self.bar(date(2020,1,8)),previous_close=10)
        cash=account.cash
        with self.assertRaisesRegex(ValueError,'cancel'):
            account.record_fill('late-fill',Fill('sell','600000',Side.SELL,date(2020,1,8),50,10.,500.,5.,.5,'filled'))
        self.assertEqual(account.cash,cash)
        self.assertEqual(sum(l.shares for l in account.lots),50)

if __name__=='__main__': unittest.main()
