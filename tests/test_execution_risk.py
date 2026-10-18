from tests.fee_fixture import rules
from datetime import date
from decimal import Decimal as D
import unittest

import core.calculation as account_execution
from core.calculation import AccountLedger, AllocationLimits
from protocol import Bar, Order, Side


class OpeningRiskTests(unittest.TestCase):
    def run_order(self, shares, price, adv, *, limits=None):
        import core.calculation as execution_risk
        self.assertTrue(hasattr(execution_risk,'OpeningRiskSnapshot'),'missing opening risk contract')
        account=AccountLedger(D('100000'))
        session=account_execution.AccountExecutionSession(account,rules())
        session.submit(Order('buy','A',Side.BUY,shares,date(2020,1,2),date(2020,1,3),'entry'))
        day=date(2020,1,3)
        risk=execution_risk.OpeningRiskSnapshot(date(2020,1,2),{'A':D(str(price))},
                                              {'A':D(str(adv))},D(0),D(0))
        risk = risk.__class__(risk.liquidity_as_of, risk.raw_marks, risk.amount20,
                              risk.receivables, risk.liabilities,
                              limits or AllocationLimits(), risk.opening_volume20)
        fill,=session.execute_open(day,{'A':Bar('A',day,price,price+1,price-1,price,1000000,10000000)},
                                  {'A':10},risk=risk)
        return account,session,fill,risk

    def test_open_gap_exceeding_adv_budget_is_rejected_not_resized(self):
        account,session,fill,_=self.run_order(1200,12,1300000,
                                               limits=AllocationLimits(stock_weight=D('.4'), adv_fraction=D('.01')))
        self.assertEqual(fill.status,'rejected')
        self.assertEqual(fill.reason,'order_above_adv_limit')
        self.assertEqual(account.cash,D('100000'))
        self.assertEqual(account.lots,())
        self.assertEqual(session.pending[0].requested_shares,1200)

    def test_open_gap_exceeding_stock_limit_is_rejected(self):
        account,session,fill,_=self.run_order(3900,11,50000000,
                                               limits=AllocationLimits(stock_weight=D('.4'), adv_fraction=D('.01')))
        self.assertEqual(fill.status,'rejected')
        self.assertEqual(fill.reason,'position_above_stock_limit')
        self.assertEqual(account.cash,D('100000'))
        self.assertEqual(len(session.fills),1)

    def test_eligible_open_fills_once_and_fees_reconcile(self):
        account,session,fill,risk=self.run_order(1200,10,50000000)
        self.assertIsNone(risk.limits.stock_weight)
        self.assertIsNone(risk.limits.adv_fraction)
        self.assertEqual(fill.status,'filled')
        self.assertEqual(account.cash,D('87983'))
        day=date(2020,1,3)
        result=session.execute_open(day,{'A':Bar('A',day,10,11,9,10,1000000,10000000)}, {'A':10},risk=risk)
        self.assertEqual(result,(fill,))
        self.assertEqual(account.cash,D('87983'))
        self.assertEqual(len(session.fills),1)

    def test_missing_risk_inputs_or_current_day_liquidity_stop_before_any_fill(self):
        from core.calculation import OpeningRiskSnapshot
        day=date(2020,1,3)
        for defect in ('missing_mark','missing_adv','future_adv','negative_claim'):
            with self.subTest(defect=defect):
                account=AccountLedger(D('100000')); session=account_execution.AccountExecutionSession(account,rules())
                for symbol in ('A','B'):
                    session.submit(Order(symbol,symbol,Side.BUY,100,date(2020,1,2),day,'entry'))
                marks={'A':D(10),'B':D(10)}; adv={'A':D('50000000'),'B':D('50000000')}
                as_of=date(2020,1,2); claim=D(0)
                if defect=='missing_mark': del marks['B']
                if defect=='missing_adv': del adv['B']
                if defect=='future_adv': as_of=day
                if defect=='negative_claim': claim=D(-1)
                risk=OpeningRiskSnapshot(as_of,marks,adv,claim,D(0))
                with self.assertRaises(ValueError):
                    session.execute_open(day,{s:Bar(s,day,10,11,9,10,1000000,10000000) for s in ('A','B')},
                                         {'A':10,'B':10},risk=risk)
                self.assertEqual(account.cash,D('100000'))
                self.assertEqual(account.lots,())
                self.assertEqual(session.fills,())


if __name__=='__main__': unittest.main()
