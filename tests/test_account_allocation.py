from tests.fee_fixture import fees
from datetime import date
from decimal import Decimal as D
import unittest

import account_execution
from account_ledger import AccountLedger
from contracts import TargetPortfolio, Side, Candidate, StrategyFrame, Bar
from tests.legacy_breadth import BreadthLowAmountReversalStrategy
from execution_risk import OpeningRiskSnapshot


class AccountAllocationTests(unittest.TestCase):
    def target(self, weights, *, rebalance=True):
        return TargetPortfolio('strategy',date(2020,1,31),weights,sum(weights.values()),
                               {s:'monthly-selection' for s in weights},rebalance)

    def allocate(self, target, holdings, prices, amounts):
        import account_allocation
        self.assertTrue(hasattr(account_allocation,'allocate_target'),'missing pure target allocation')
        return account_allocation.allocate_target(target,next_session=date(2020,2,3),
                   equity=D('100000'),holdings=holdings,raw_closes=prices,amount20=amounts)

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
        target=TargetPortfolio('strategy',date(2020,2,3),{},0,{},False)
        second=session.apply_target(target,next_session=date(2020,2,4),equity=D('100000'),raw_closes={},amount20={})
        self.assertEqual(second.orders,())
        self.assertEqual(session.pending,pending)
        self.assertEqual(session.account.order_cancellations,())
        session.apply_target(TargetPortfolio('strategy',date(2020,2,4),{},0,{'__market__':'market-exit'}),
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
            session.apply_target(TargetPortfolio('strategy',date(2020,2,3),{'B':.125},.125,{}),
                                 next_session=date(2020,2,4),equity=D('100000'),raw_closes={},amount20={})
        self.assertEqual(session.pending,pending)
        self.assertEqual(session.account.order_cancellations,())
        weights['A']=.25
        with self.assertRaisesRegex(ValueError,'conflicting target replay'): session.apply_target(target,**kwargs)

    def test_original_strategy_eight_stock_entry_and_breadth_exit_reconcile_and_repeat(self):
        def run():
            account=AccountLedger(D('100000'))
            session=account_execution.AccountExecutionSession(account,fees())
            strategy=BreadthLowAmountReversalStrategy()
            candidates=tuple(Candidate(f'{i:06d}',None,None,None,
                60000000 if i<=8 else 100000000,12,10,None,r20=-.2+i*.001)
                for i in range(1,81))
            frame=StrategyFrame(date(2020,1,31),1,candidates,held_days={},market_on=True,
                                market_breadth_120_smoothed=.60,is_month_end=True)
            target=strategy.target(frame)
            symbols=tuple(f'{i:06d}' for i in range(1,9))
            self.assertEqual(target.target_weights,{s:.125 for s in symbols})
            prices={s:D('10') for s in symbols}; amounts={s:D('60000000') for s in symbols}
            entry=session.apply_target(target,next_session=date(2020,2,3),equity=D('100000'),
                                       raw_closes=prices,amount20=amounts)
            self.assertEqual([o.requested_shares for o in entry.orders],[1200]*8)
            def bars(day):
                return {s:Bar(s,day,10,11,9,10,6000000,60000000) for s in symbols}
            session.execute_open(date(2020,2,3),bars(date(2020,2,3)),{s:10 for s in symbols},
                                 risk=OpeningRiskSnapshot(date(2020,1,31),prices,amounts,D(0),D(0)))
            point=account.mark(date(2020,2,3),prices,D(0),D(0))
            self.assertEqual(point.cash,D('3864'))
            self.assertEqual(point.market_value,D('96000'))
            self.assertEqual(point.equity,D('99864'))
            exit_target=strategy.target(StrategyFrame(date(2020,2,4),0,candidates,
                held_days={s:2 for s in symbols},market_on=False,
                market_breadth_120_smoothed=.44,is_month_end=False))
            exit_plan=session.apply_target(exit_target,next_session=date(2020,2,5),equity=point.equity,
                                           raw_closes=prices,amount20=amounts)
            self.assertEqual([o.side for o in exit_plan.orders],[Side.SELL]*8)
            session.execute_open(date(2020,2,5),bars(date(2020,2,5)),{s:10 for s in symbols},
                                 risk=OpeningRiskSnapshot(date(2020,2,4),prices,amounts,D(0),D(0)))
            final=account.mark(date(2020,2,5),{},D(0),D(0))
            self.assertEqual(final.cash,D('99632.096'))
            self.assertEqual(final.equity,final.cash)
            self.assertEqual(final.realized_pnl,D('-367.904'))
            self.assertEqual(final.unrealized_pnl,D(0))
            self.assertEqual(account.lots,())
            self.assertEqual(len(session.fills),16)
            return (target,exit_target,entry,exit_plan,session.fills,account.cash_events,point,final)
        self.assertEqual(run(),run())


if __name__=='__main__': unittest.main()
