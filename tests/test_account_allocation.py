from tests.fee_fixture import fees
from datetime import UTC, date, datetime
from decimal import Decimal as D
import unittest

import account_execution
from account_ledger import AccountLedger
from protocal import Side, TargetSet
from execution_risk import OpeningRiskSnapshot


class AccountAllocationTests(unittest.TestCase):
    def target(self, weights, *, rebalance=True):
        return TargetSet('strategy',datetime(2020,1,31,tzinfo=UTC),weights,
                         {s:'monthly-selection' for s in weights})

    def allocate(self, target, holdings, prices, amounts):
        import account_allocation
        self.assertTrue(hasattr(account_allocation,'allocate_target'),'missing pure target allocation')
        return account_allocation.allocate_target(target,next_session=date(2020,2,3),
                   equity=D('100000'),holdings=holdings,raw_closes=prices,amount20=amounts,
                   rebalance=True)

    def test_monthly_target_sizes_raw_prices_and_records_every_difference(self):
        plan=self.allocate(self.target({'B':.125}),{'A':150},
                           {'A':D('10'),'B':D('12')},{'A':D('50000000'),'B':D('50000000')})
        self.assertEqual([(o.symbol,o.side,o.requested_shares) for o in plan.orders],
                         [('A',Side.SELL,150),('B',Side.BUY,1000)])
        self.assertEqual([(d.symbol,d.target_shares,d.delta,d.status) for d in plan.decisions],
                         [('A',0,-150,'ordered'),('B',1000,1000,'ordered')])
        self.assertEqual(plan,self.allocate(self.target({'B':.125}),{'A':150},
                           {'B':D('12'),'A':D('10')},{'B':D('50000000'),'A':D('50000000')}))

    def test_cap_and_unaffordable_lot_skip_with_explicit_reasons_not_resize(self):
        plan=self.allocate(self.target({'A':.125,'B':.125,'C':.5}),{},
                           {'A':D('1000'),'B':D('10'),'C':D('10')},
                           {'A':D('50000000'),'B':D('100000'),'C':D('50000000')})
        self.assertEqual(plan.orders,())
        self.assertEqual([(d.symbol,d.reason) for d in plan.decisions],
                         [('A','below_one_lot'),('B','order_above_adv_limit'),('C','weight_above_stock_limit')])

    def test_odd_holdings_do_not_create_an_unfillable_odd_buy_order(self):
        plan=self.allocate(self.target({'A':.125}),{'A':150},
                           {'A':D('12')},{'A':D('50000000')})
        self.assertEqual(plan.decisions[0].target_shares,1000)
        self.assertEqual(plan.decisions[0].delta,850)
        self.assertEqual(plan.orders[0].requested_shares,800)
        small=self.allocate(self.target({'A':.125}),{'A':950},
                            {'A':D('12')},{'A':D('50000000')})
        self.assertEqual(small.orders,())
        self.assertEqual(small.decisions[0].reason,'below_one_lot')

    def test_incomplete_price_or_liquidity_and_invalid_weights_stop_allocation(self):
        for prices,amounts in (({}, {'A':D('1')}),({'A':D('10')},{}),
                              ({'A':D('NaN')},{'A':D('1')})):
            with self.assertRaises(ValueError): self.allocate(self.target({'A':.125}),{},prices,amounts)
        with self.assertRaises(ValueError):
            self.allocate(self.target({'A':float('nan')}),{}, {'A':D('10')},{'A':D('1000000')})

    def test_observation_only_target_does_not_cancel_pending(self):
        self.assertTrue(hasattr(account_execution.AccountExecutionSession,'apply_target'),
                        'missing target to account orchestration')
        session=account_execution.AccountExecutionSession(AccountLedger(D('100000')),fees())
        first=session.apply_target(self.target({'A':.125}),next_session=date(2020,2,3),
                                   equity=D('100000'),raw_closes={'A':D('10')},amount20={'A':D('50000000')})
        self.assertEqual(len(session.pending),1)
        pending=session.pending
        target=TargetSet('strategy',datetime(2020,2,3,tzinfo=UTC),{}, {})
        second=session.apply_target(target,next_session=date(2020,2,4),equity=D('100000'),raw_closes={},amount20={},rebalance=False)
        self.assertEqual(second.orders,())
        self.assertEqual(session.pending,pending)
        self.assertEqual(session.account.order_cancellations,())
        session.apply_target(TargetSet('strategy',datetime(2020,2,4,tzinfo=UTC),{}, {'__market__':'market-exit'}),
                             next_session=date(2020,2,5),equity=D('100000'),raw_closes={},amount20={})
        self.assertEqual(session.pending,())
        self.assertEqual(len(session.account.order_cancellations),1)
        self.assertEqual(session.account.order_cancellations[0].order_id,first.orders[0].order_id)

    def test_bad_replacement_preserves_old_order_and_replay_cannot_mutate_target(self):
        session=account_execution.AccountExecutionSession(AccountLedger(D('100000')),fees())
        weights={'A':.125}; target=self.target(weights)
        kwargs=dict(next_session=date(2020,2,3),equity=D('100000'),
                    raw_closes={'A':D('10')},amount20={'A':D('50000000')})
        plan=session.apply_target(target,**kwargs)
        self.assertEqual(session.apply_target(target,**kwargs),plan)
        self.assertEqual(len(session.orders),1)
        pending=session.pending
        with self.assertRaises(ValueError):
            session.apply_target(TargetSet('strategy',datetime(2020,2,3,tzinfo=UTC),{'B':.125},{}),
                                 next_session=date(2020,2,4),equity=D('100000'),raw_closes={},amount20={})
        self.assertEqual(session.pending,pending)
        self.assertEqual(session.account.order_cancellations,())
        weights['A']=.25
        self.assertEqual(target.weights['A'],.125)
        self.assertEqual(session.apply_target(target,**kwargs),plan)
        conflicting=TargetSet('strategy',datetime(2020,1,31,tzinfo=UTC),{'A':.25},
                              {'A':'monthly-selection'})
        with self.assertRaisesRegex(ValueError,'conflicting target replay'):
            session.apply_target(conflicting,**kwargs)

if __name__=='__main__': unittest.main()
