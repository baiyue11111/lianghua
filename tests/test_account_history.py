from tests.fee_fixture import fees, tax_policy
from dataclasses import replace
from datetime import date, datetime, UTC, timedelta
from decimal import Decimal as D
import unittest

import account_history
from contracts import Bar, Side
from core.contracts import MarketFrame, HistoryBar
from corporate_actions import normalize_dividend_records
from plugins.strategies.breadth_low_amount_reversal import Strategy, Params


class AccountHistoryTests(unittest.TestCase):
    calendar=(date(2020,1,31),date(2020,2,3),date(2020,2,4),date(2020,2,5),date(2020,2,6))

    def inputs(self, exit_market=True):
        self.assertTrue(hasattr(account_history,'run_account_history'),'missing chronological account runner')
        symbols=tuple(f'{i:06d}' for i in range(1,81))
        records=[]
        for index,day in enumerate(self.calendar[:-1]):
            on=not exit_market or index<2
            at=datetime.combine(day,datetime.min.time(),tzinfo=UTC)
            history={s:tuple(HistoryBar(at-timedelta(days=125-i),{
                "open":10.,"high":11.,"low":9.,"close":10.,"volume":6_000_000.,
                "amount":60_000_000. if int(s)<=8 else 100_000_000.,
                "signal_close":10. if i<125 else 12.+int(s)/10000 if on else 8.,
            }) for i in range(126)) for s in symbols}
            frame=MarketFrame(at,"CN_EQUITY",{}, {"next_session":"2099-01-01"},history)
            records.append(account_history.HistoryDay(day,frame,
                {s:Bar(s,day,10,11,9,10,6000000,60000000) for s in symbols},
                {s:10 for s in symbols},{s:D(10) for s in symbols},{s:D(10) for s in symbols},
                {s:D('60000000') for s in symbols},{s:D('60000000') for s in symbols},
                date(2020,1,30) if index==0 else self.calendar[index-1],
                ()))
        return tuple(records)

    def run_history(self, records, calendar=None):
        return account_history.run_account_history(Strategy(Params(breadth_window=1)),
            calendar=self.calendar if calendar is None else calendar,days=iter(records),initial_cash=D('100000'),fees=fees(),tax_policy=tax_policy())

    def test_original_strategy_runs_next_open_and_repeats_complete_account_trace(self):
        first=self.run_history(self.inputs()); second=self.run_history(self.inputs())
        self.assertEqual(first,second)
        self.assertEqual([p.cash for p in first.equity],[D('100000'),D('3864'),D('3864'),D('99632.096')])
        self.assertEqual([p.equity for p in first.equity],[D('100000'),D('99864'),D('99864'),D('99632.096')])
        self.assertEqual(len(first.fills),16)
        self.assertEqual({f.trade_date for f in first.fills if f.side is Side.BUY},{date(2020,2,3)})
        self.assertEqual({f.trade_date for f in first.fills if f.side is Side.SELL},{date(2020,2,5)})
        self.assertIsNone(first.targets[1])
        self.assertEqual(first.pending,())
        self.assertEqual(first.holdings[-1][1],())
        self.assertEqual(first.frames[1].context["next_session"],"2020-02-04")

    def test_registration_distribution_and_potential_tax_use_same_account(self):
        records=list(self.inputs(exit_market=False))
        event=normalize_dividend_records([{'code':'000001','dividRegistDate':'2020-02-03',
            'dividOperateDate':'2020-02-04','dividPayDate':'2020-02-05',
            'dividStockMarketDate':'2020-02-06','dividCashPsBeforeTax':'0.3','dividStocksPs':'0.1'}])[0]
        action=account_history.RegistrationAction('plan',event,'a'*64,date(2020,2,5),D(1),D(0),'b'*64)
        records[1]=replace(records[1],registrations=(action,))
        result=self.run_history(records)
        self.assertEqual(result.equity[2].receivables,D('1560'))
        self.assertEqual(result.equity[3].receivables,D(0))
        self.assertEqual(result.equity[3].cash,D('4224'))
        self.assertEqual(sum(l.shares for l in result.holdings[-1][1]),9720)
        bonus=[l for l in result.holdings[-1][1] if l.cost==0]
        self.assertEqual(len(bonus),1)
        self.assertEqual(bonus[0].shares,120)
        self.assertEqual(bonus[0].tradable_from,date(2020,2,6))
        self.assertEqual(len(result.distribution_postings),2)
        self.assertEqual(len([p for p in result.cash_events if p.kind=='dividend_tax']),0)
        self.assertAlmostEqual(result.equity[-1].liabilities,D(96),places=24)

    def test_incomplete_calendar_mismatched_frames_and_missing_liability_never_return_result(self):
        records=self.inputs()
        with self.assertRaises(ValueError): self.run_history(records[:-1])
        with self.assertRaises(ValueError): self.run_history(records,calendar=self.calendar[:-1])
        bad=replace(records[0],frame=replace(records[0].frame,at=datetime(2020,2,1,tzinfo=UTC)))
        with self.assertRaises(ValueError): self.run_history((bad,*records[1:]))

    def test_record_trade_date_must_match_declared_calendar(self):
        records=list(self.inputs())
        records[1]=replace(records[1],trade_date=date(2020,2,4),
                           frame=replace(records[1].frame,at=datetime(2020,2,4,tzinfo=UTC)))
        with self.assertRaises(account_history.HistoryRunError) as caught:
            self.run_history(records)
        self.assertEqual(caught.exception.failed_day,date(2020,2,3))

    def test_settlement_failure_cannot_continue_but_keeps_partial_result(self):
        from plugins.data import Settlement
        records=self.inputs()
        settlement=Settlement(D('100000'),fees(),tax_policy())
        settlement.apply(records[0],self.calendar[1])
        settlement.plan(None,'breadth_low_amount_reversal')
        bad=replace(records[1],frame=replace(records[1].frame,at=datetime(2020,2,4,tzinfo=UTC)))
        with self.assertRaises(ValueError): settlement.apply(bad,self.calendar[2])
        partial=settlement.result()
        self.assertEqual(len(partial.equity),1)
        with self.assertRaisesRegex(ValueError,'failed'):
            settlement.apply(records[2],self.calendar[3])
        with self.assertRaisesRegex(ValueError,'failed'):
            settlement.plan(None,'breadth_low_amount_reversal')

    def test_settlement_requires_previous_following_session(self):
        from plugins.data import Settlement
        records=self.inputs()
        settlement=Settlement(D('100000'),fees(),tax_policy())
        settlement.apply(records[0],self.calendar[1])
        settlement.plan(None,'breadth_low_amount_reversal')
        with self.assertRaises(ValueError): settlement.apply(records[2],self.calendar[3])

    def test_settlement_plan_failure_is_terminal_with_partial_evidence(self):
        from plugins.data import Settlement
        records=self.inputs()
        settlement=Settlement(D('100000'),fees(),tax_policy())
        settlement.apply(records[0],self.calendar[1])
        with self.assertRaises(ValueError): settlement.plan(None, '')
        self.assertEqual(len(settlement.result().equity), 0)
        with self.assertRaisesRegex(ValueError, 'failed'):
            settlement.plan(None, 'breadth_low_amount_reversal')

    def test_window_end_does_not_force_liquidation_or_create_month_end(self):
        result=self.run_history(self.inputs()[:2],calendar=self.calendar[:3])
        self.assertEqual(len(result.fills),8)
        self.assertEqual(sum(l.shares for l in result.holdings[-1][1]),9600)
        self.assertEqual(result.frames[-1].context["next_session"],"2020-02-04")
        self.assertIsNone(result.targets[-1])

    def test_midrun_failure_retains_failed_account_trace_without_success_verdict(self):
        records=list(self.inputs())
        records[2]=replace(records[2],frame=replace(records[2].frame,at=datetime(2020,2,5,tzinfo=UTC)))
        self.assertTrue(hasattr(account_history,'HistoryRunError'),'failed runs must retain account evidence')
        with self.assertRaises(account_history.HistoryRunError) as caught: self.run_history(records)
        failure=caught.exception
        self.assertEqual(failure.failed_day,date(2020,2,4))
        self.assertEqual(failure.cash,D('3864'))
        self.assertEqual(sum(l.shares for l in failure.lots),9600)
        self.assertEqual(len(failure.partial_trace.fills),8)
        self.assertEqual(len(failure.partial_trace.equity),2)
        self.assertEqual(len(failure.partial_trace.orders),8)

    def test_warmup_targets_do_not_execute_and_first_active_target_is_retained(self):
        from plugins.strategies.moving_average_cross import Strategy as Cross, Params as CrossParams
        records=self.inputs(exit_market=False)
        result=account_history.run_account_history(Cross(CrossParams(1,2)),calendar=self.calendar,
            days=records,initial_cash=D('100000'),fees=fees(),tax_policy=tax_policy(),trade_start=date(2020,2,3))
        self.assertIsNone(result.targets[0])
        self.assertFalse(result.frames[0].context['trading'])
        self.assertTrue(result.frames[1].context['trading'])
        self.assertIsNotNone(result.targets[1])
        self.assertTrue(result.fills)
        self.assertTrue(all(fill.trade_date>=date(2020,2,4) for fill in result.fills))

    def test_sell_tax_reserve_limits_same_open_buy_budget(self):
        from account_execution import AccountExecutionSession
        from account_ledger import AccountLedger
        from contracts import Order, Fill
        from tax_ledger import TaxLedger
        from execution_risk import OpeningRiskSnapshot
        from account_allocation import AllocationLimits
        account=AccountLedger(D(1000)); session=AccountExecutionSession(account,fees()); taxes=TaxLedger(tax_policy())
        buy=Order('initial','A',Side.BUY,100,date(2020,1,1),date(2020,1,2),'entry')
        account.register_order(buy)
        first=Fill('initial','A',Side.BUY,date(2020,1,2),100,10,1000,0,0,'filled')
        account.record_fill('first',first); taxes.begin(first.trade_date); taxes.preview(first); taxes.end((first,))
        event=normalize_dividend_records([{'code':'A','dividRegistDate':'2020-01-10','dividOperateDate':'2020-01-13','dividPayDate':'2020-01-20','dividCashPsBeforeTax':'1'}])[0]
        taxes.register('div',event,D(1),D(0),'a'*64)
        day=date(2020,1,21); taxes.begin(day)
        session.submit(Order('sell','A',Side.SELL,100,date(2020,1,20),day,'exit'))
        session.submit(Order('buy','B',Side.BUY,100,date(2020,1,20),day,'entry'))
        bars={'A':Bar('A',day,10,11,9,10,1000000,10000000),'B':Bar('B',day,9.7,11,9,10,1000000,10000000)}
        risk=OpeningRiskSnapshot(date(2020,1,20),{'A':D(10),'B':D('9.7')},{'A':D(10000000),'B':D(10000000)},D(0),D(20),AllocationLimits(stock_weight=D(1)))
        fills=session.execute_open(day,bars,{'A':10.,'B':10.},risk=risk,cash_reserve=lambda:taxes.reserve(day),on_fill=taxes.preview)
        self.assertEqual([f.status for f in fills],['filled','rejected'])
        taxes.end(fills)
        for payment in taxes.settle(day): account.record_cash(payment.event_id,day,'dividend_tax',-payment.amount,payment.source_sha256)
        self.assertEqual(account.cash,D('973.001'))

    def test_weekend_income_share_credit_and_unpaid_tax_settle_by_actual_dates(self):
        from core.contracts import TargetSet
        class Plan:
            strategy_id='weekend'
            def generate(self,frame,account):
                day=frame.at.date()
                if day==date(2020,7,1): return TargetSet(self.strategy_id,frame.at,{'A':.1},{'A':'entry'})
                if day==date(2020,7,2): return TargetSet(self.strategy_id,frame.at,{}, {'__market__':'exit'})
                return None
        calendar=(date(2020,7,1),date(2020,7,2),date(2020,7,3),date(2020,7,6),date(2020,7,7))
        records=[]
        event=normalize_dividend_records([{'code':'A','dividRegistDate':'2020-07-02','dividOperateDate':'2020-07-03','dividPayDate':'2020-07-04','dividStockMarketDate':'2020-07-06','dividCashPsBeforeTax':'.3','dividStocksPs':'.1'}])[0]
        action=account_history.RegistrationAction('weekend',event,'a'*64,date(2020,7,5),D(1),D(0),'b'*64)
        for index,day in enumerate(calendar[:-1]):
            frame=MarketFrame(datetime.combine(day,datetime.min.time(),tzinfo=UTC),'CN_EQUITY',{})
            records.append(account_history.HistoryDay(day,frame,{'A':Bar('A',day,10,11,9,10,1000000,10000000)},
                {'A':10.},{'A':D(10)},{'A':D(10)},{'A':D(100000000)},{'A':D(100000000)},
                date(2020,6,30) if index==0 else calendar[index-1],(action,) if day==date(2020,7,2) else ()))
        result=account_history.run_account_history(Plan(),calendar=calendar,days=records,initial_cash=D(10000),fees=fees(),tax_policy=tax_policy())
        self.assertEqual(result.equity[2].liabilities,D(8))
        self.assertEqual(result.equity[2].receivables,D(130))
        self.assertEqual([(p.day,p.cash_delta) for p in result.cash_events if p.kind=='dividend_tax'],[(date(2020,7,4),D(-6)),(date(2020,7,5),D(-2))])
        self.assertEqual(result.equity[-1].cash,D('10009.001'))
        self.assertEqual(result.equity[-1].liabilities,D(0))
        self.assertEqual(result.holdings[-1][1][0].shares,10)
