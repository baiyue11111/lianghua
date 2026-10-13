from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
import unittest

from core.calculation import AllocationLimits
from core.calculation import AccountExecutionSession
from core.calculation import AccountLedger
from core.calculation import normalize_dividend_records
from core.calculation import OpeningRiskSnapshot
from plugins.data import Settlement
from plugins.strategies.moving_average_cross import Params as CrossParams, Strategy as Cross
from protocal import Bar, Fill, HistoryDay, MarketFrame, Order, RegistrationAction, Side, TargetSet
from core.calculation import TaxLedger
from tests.fee_fixture import rules, tax_policy, settlement as make_settlement
from tests.settlement_fixture import SettlementFixture


class SettlementFlowTests(unittest.TestCase):
    def setUp(self):
        self.case = SettlementFixture()

    def test_next_open_and_trace_are_stable(self):
        first = self.case.run_settlement(self.case.inputs())
        second = self.case.run_settlement(self.case.inputs())
        self.assertEqual(first, second)
        self.assertEqual([point.cash for point in first.equity], [D('100000'), D('3864'), D('3864'), D('99632.096')])
        self.assertEqual([point.equity for point in first.equity], [D('100000'), D('99864'), D('99864'), D('99632.096')])
        self.assertEqual(len(first.fills), 16)
        self.assertEqual({fill.trade_date for fill in first.fills if fill.side is Side.BUY}, {date(2020, 2, 3)})
        self.assertEqual({fill.trade_date for fill in first.fills if fill.side is Side.SELL}, {date(2020, 2, 5)})
        self.assertIsNone(first.targets[1])
        self.assertEqual(first.pending, ())
        self.assertEqual(first.holdings[-1][1], ())
        self.assertEqual(first.frames[1].context['next_session'], '2020-02-04')

    def test_registration_distribution_and_tax_use_the_same_account(self):
        records = list(self.case.inputs(exit_market=False))
        event = normalize_dividend_records([{
            'code': '000001', 'dividRegistDate': '2020-02-03', 'dividOperateDate': '2020-02-04',
            'dividPayDate': '2020-02-05', 'dividStockMarketDate': '2020-02-06',
            'dividCashPsBeforeTax': '0.3', 'dividStocksPs': '0.1',
        }])[0]
        records[1] = replace(records[1], registrations=(RegistrationAction(
            'plan', event, 'a' * 64, date(2020, 2, 5), D(1), D(0), 'b' * 64
        ),))
        result = self.case.run_settlement(records)
        self.assertEqual(result.equity[2].receivables, D('1560'))
        self.assertEqual(result.equity[3].receivables, D(0))
        self.assertEqual(result.equity[3].cash, D('4224'))
        self.assertEqual(sum(lot.shares for lot in result.holdings[-1][1]), 9720)
        self.assertEqual(len(result.distribution_postings), 2)
        self.assertAlmostEqual(result.equity[-1].liabilities, D(96), places=24)

    def test_calendar_and_trade_date_validation_is_explicit(self):
        records = self.case.inputs()
        with self.assertRaises(ValueError):
            self.case.run_settlement(records[:-1])
        with self.assertRaises(ValueError):
            self.case.run_settlement(records, calendar=self.case.calendar[:-1])
        bad = replace(records[0], frame=replace(records[0].frame, at=datetime(2020, 2, 1, tzinfo=UTC)))
        with self.assertRaises(ValueError):
            self.case.run_settlement((bad, *records[1:]))
        bad_trade = replace(records[1], trade_date=date(2020, 2, 4), frame=replace(records[1].frame, at=datetime(2020, 2, 4, tzinfo=UTC)))
        settlement = make_settlement(D('100000'))
        settlement.apply(records[0], self.case.calendar[1])
        settlement.plan(None, 'breadth_low_amount_reversal')
        with self.assertRaises(ValueError):
            settlement.apply(bad_trade, self.case.calendar[3])

    def test_failed_settlement_keeps_partial_evidence_and_is_terminal(self):
        records = self.case.inputs()
        settlement = make_settlement(D('100000'))
        settlement.apply(records[0], self.case.calendar[1])
        settlement.plan(None, 'breadth_low_amount_reversal')
        bad = replace(records[1], frame=replace(records[1].frame, at=datetime(2020, 2, 4, tzinfo=UTC)))
        with self.assertRaises(ValueError):
            settlement.apply(bad, self.case.calendar[2])
        partial = settlement.result()
        self.assertEqual(len(partial.equity), 1)
        self.assertEqual(partial.equity[0].day, records[0].trade_date)
        self.assertEqual(partial.equity[0].cash, D('100000'))
        self.assertEqual(partial.equity[0].equity, D('100000'))
        self.assertEqual(partial.holdings[0][1], ())
        self.assertEqual(partial.orders, ())
        self.assertEqual(partial.fills, ())
        self.assertEqual(partial.pending, ())
        with self.assertRaisesRegex(ValueError, 'failed'):
            settlement.apply(records[2], self.case.calendar[3])
        with self.assertRaisesRegex(ValueError, 'failed'):
            settlement.plan(None, 'breadth_low_amount_reversal')

    def test_failed_trace_is_saved_under_the_failed_json_path(self):
        import json
        from tempfile import TemporaryDirectory

        from protocal import save_failed_backtest
        from protocal import BacktestError, BacktestRequest, build_data

        settlement = make_settlement(D('100000'))
        settlement.apply(self.case.inputs()[0], self.case.calendar[1])
        settlement.plan(None, 'breadth_low_amount_reversal')
        partial = settlement.result()
        request = BacktestRequest(
            'failed-trace', 'plugins.strategies.moving_average_cross', {}, 'fixture',
            datetime(2020, 2, 3, tzinfo=UTC), datetime(2020, 2, 3, tzinfo=UTC), D('100000'),
        )
        error = BacktestError('run_failed', 'bad historical day')
        with TemporaryDirectory() as root:
            value = save_failed_backtest(build_data(Path(root)), request, error, partial)
            self.assertEqual(value, 'data/runs/failed-trace/failed.json')
            saved = json.loads((Path(root) / value).read_text())
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['partial']['equity'][0]['day'], '2020-01-31')
        self.assertEqual(saved['partial']['equity'][0]['cash'], '100000')
        self.assertEqual(saved['partial']['holdings'][0][1], [])
        self.assertEqual(saved['partial']['orders'], [])
        self.assertEqual(saved['partial']['fills'], [])
        self.assertEqual(saved['partial']['pending'], [])

    def test_previous_following_session_is_required_and_window_end_does_not_liquidate(self):
        records = self.case.inputs()
        settlement = make_settlement(D('100000'))
        settlement.apply(records[0], self.case.calendar[1])
        settlement.plan(None, 'breadth_low_amount_reversal')
        with self.assertRaises(ValueError):
            settlement.apply(records[2], self.case.calendar[3])
        result = self.case.run_settlement(records[:2], calendar=self.case.calendar[:3])
        self.assertEqual(len(result.fills), 8)
        self.assertEqual(sum(lot.shares for lot in result.holdings[-1][1]), 9600)
        self.assertEqual(result.frames[-1].context['next_session'], '2020-02-04')
        self.assertIsNone(result.targets[-1])

    def test_plan_failure_has_no_success_result(self):
        settlement = make_settlement(D('100000'))
        settlement.apply(self.case.inputs()[0], self.case.calendar[1])
        with self.assertRaises(ValueError):
            settlement.plan(None, '')
        self.assertEqual(len(settlement.result().equity), 0)
        with self.assertRaisesRegex(ValueError, 'failed'):
            settlement.plan(None, 'breadth_low_amount_reversal')

    def test_warmup_target_is_not_executed(self):
        result = self.case.run_settlement(
            self.case.inputs(exit_market=False),
            strategy=Cross(CrossParams(1, 2)),
            trade_start=date(2020, 2, 3),
        )
        self.assertIsNone(result.targets[0])
        self.assertFalse(result.frames[0].context['trading'])
        self.assertTrue(result.frames[1].context['trading'])
        self.assertIsNotNone(result.targets[1])
        self.assertTrue(result.fills)
        self.assertTrue(all(fill.trade_date >= date(2020, 2, 4) for fill in result.fills))

    def test_tax_reserve_limits_same_open_buy_budget(self):
        account = AccountLedger(D(1000))
        trade_rules = rules()
        session = AccountExecutionSession(account, trade_rules)
        taxes = trade_rules._tax_ledger
        buy = Order('initial', 'A', Side.BUY, 100, date(2020, 1, 1), date(2020, 1, 2), 'entry')
        account.register_order(buy)
        first = Fill('initial', 'A', Side.BUY, date(2020, 1, 2), 100, 10, 1000, 0, 0, 'filled')
        account.record_fill('first', first)
        trade_rules.tax(first.trade_date, phase='begin')
        trade_rules.tax(first.trade_date, (first,), phase='preview')
        trade_rules.tax(first.trade_date, (first,), phase='end')
        event = normalize_dividend_records([{'code': 'A', 'dividRegistDate': '2020-01-10', 'dividOperateDate': '2020-01-13', 'dividPayDate': '2020-01-20', 'dividCashPsBeforeTax': '1'}])[0]
        taxes.register('div', event, D(1), D(0), 'a' * 64)
        day = date(2020, 1, 21)
        trade_rules.tax(day, phase='begin')
        session.submit(Order('sell', 'A', Side.SELL, 100, date(2020, 1, 20), day, 'exit'))
        session.submit(Order('buy', 'B', Side.BUY, 100, date(2020, 1, 20), day, 'entry'))
        bars = {'A': Bar('A', day, 10, 11, 9, 10, 1000000, 10000000), 'B': Bar('B', day, 9.7, 11, 9, 10, 1000000, 10000000)}
        risk = OpeningRiskSnapshot(date(2020, 1, 20), {'A': D(10), 'B': D('9.7')}, {'A': D(10000000), 'B': D(10000000)}, D(0), D(20), AllocationLimits(stock_weight=D(1)))
        fills = session.execute_open(day, bars, {'A': 10., 'B': 10.}, risk=risk,
                                     cash_reserve=D(0))
        self.assertEqual([item.status for item in fills], ['filled', 'rejected'])
        trade_rules.tax(day, fills, phase='end')
        for payment in taxes.settle(day):
            account.record_cash(payment.event_id, day, 'dividend_tax', -payment.amount, payment.source_sha256)
        self.assertEqual(account.cash, D('973.001'))

    def test_weekend_income_share_credit_and_tax_use_actual_dates(self):
        class Plan:
            strategy_id = 'weekend'

            def generate(self, frame, account):
                if frame.at.date() == date(2020, 7, 1):
                    return TargetSet(self.strategy_id, frame.at, {'A': .1}, {'A': 'entry'})
                if frame.at.date() == date(2020, 7, 2):
                    return TargetSet(self.strategy_id, frame.at, {}, {'__market__': 'exit'})
                return None

        calendar = (date(2020, 7, 1), date(2020, 7, 2), date(2020, 7, 3), date(2020, 7, 6), date(2020, 7, 7))
        event = normalize_dividend_records([{'code': 'A', 'dividRegistDate': '2020-07-02', 'dividOperateDate': '2020-07-03', 'dividPayDate': '2020-07-04', 'dividStockMarketDate': '2020-07-06', 'dividCashPsBeforeTax': '.3', 'dividStocksPs': '.1'}])[0]
        action = RegistrationAction('weekend', event, 'a' * 64, date(2020, 7, 5), D(1), D(0), 'b' * 64)
        records = []
        for index, day in enumerate(calendar[:-1]):
            frame = MarketFrame(datetime.combine(day, datetime.min.time(), tzinfo=UTC), 'CN_EQUITY', {})
            records.append(HistoryDay(day, frame, {'A': Bar('A', day, 10, 11, 9, 10, 1000000, 10000000)}, {'A': 10.}, {'A': D(10)}, {'A': D(10)}, {'A': D(100000000)}, {'A': D(100000000)}, date(2020, 6, 30) if index == 0 else calendar[index - 1], (action,) if day == date(2020, 7, 2) else ()))
        settlement = make_settlement(D(10000))
        for index, record in enumerate(records):
            frame, account = settlement.apply(record, calendar[index + 1])
            settlement.plan(Plan().generate(frame, account), 'weekend')
        result = settlement.result()
        self.assertEqual(result.equity[2].liabilities, D(8))
        self.assertEqual(result.equity[2].receivables, D(130))
        self.assertEqual([(item.day, item.cash_delta) for item in result.cash_events if item.kind == 'dividend_tax'], [(date(2020, 7, 4), D(-6)), (date(2020, 7, 5), D(-2))])
        self.assertEqual(result.equity[-1].cash, D('10009.001'))
        self.assertEqual(result.equity[-1].liabilities, D(0))
        self.assertEqual(result.holdings[-1][1][0].shares, 10)
