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
from protocol import Bar, Fill, HistoryDay, MarketFrame, Order, RegistrationAction, Side, TargetSet
from core.calculation import TaxLedger
from tests.fee_fixture import rules, tax_policy, settlement as make_settlement
from tests.settlement_fixture import SettlementFixture


class SettlementFlowTests(unittest.TestCase):
    def test_research_rejects_slipped_fill_over_adv_limit(self):
        from core.calculation import AllocationLimits
        from tests.fee_fixture import execution, fees

        config = execution()
        config['tax'] = {'model': 'none', 'params': {}}
        book = Settlement(D('2000'), fees(), tax_policy(),
                          limits=AllocationLimits(adv_fraction=D('.01')),
                          execution=config, accounting='research')
        source = SettlementFixture().inputs(exit_market=False)
        records = []
        for record in source[:2]:
            symbol = '000001'
            bar = replace(record.bars[symbol], high=10.02, low=9.99, volume=1000000)
            frame = replace(record.frame, context={**record.frame.context,
                'back_factors': {symbol: '1'}})
            records.append(replace(record, frame=frame, bars={symbol: bar},
                previous_closes={symbol: 10}, opening_marks={symbol: D(10)},
                closing_marks={symbol: D(10)}, opening_amount20={symbol: D(100000)},
                closing_amount20={symbol: D(100000)},
                opening_volume20={symbol: D(1000000)},
                opening_eligible=None, registrations=()))

        frame, _ = book.apply(records[0], self.case.calendar[1])
        book.plan(TargetSet('test', frame.at, {'000001': .5}), 'test')
        before = book.result().equity[-1].equity
        book.apply(records[1], self.case.calendar[2])

        fill = book.result().fills[-1]
        self.assertEqual((fill.status, fill.reason), ('rejected', 'order_above_adv_limit'))
        self.assertEqual(fill.shares, 0)
        self.assertEqual(book._research.cash, D(2000))
        self.assertEqual(book._research.units, {})
        self.assertEqual(book._research.pending_symbols, {'000001'})
        self.assertEqual(book.result().equity[-1].equity, before)

    def test_research_adv_boundary_excludes_fees(self):
        from core.calculation import AllocationLimits
        from tests.fee_fixture import execution, fees

        config = execution()
        config['tax'] = {'model': 'none', 'params': {}}
        config['price']['params']['rate'] = '0'
        book = Settlement(D('2000'), fees(), tax_policy(),
                          limits=AllocationLimits(adv_fraction=D('.01')),
                          execution=config, accounting='research')
        source = SettlementFixture().inputs(exit_market=False)
        records = []
        for record in source[:2]:
            symbol = '000001'
            bar = replace(record.bars[symbol], high=10.02, low=9.99, volume=1000000)
            frame = replace(record.frame, context={**record.frame.context,
                'back_factors': {symbol: '1'}})
            records.append(replace(record, frame=frame, bars={symbol: bar},
                previous_closes={symbol: 10}, opening_marks={symbol: D(10)},
                closing_marks={symbol: D(10)}, opening_amount20={symbol: D(100000)},
                closing_amount20={symbol: D(100000)},
                opening_volume20={symbol: D(1000000)},
                opening_eligible=None, registrations=()))

        frame, _ = book.apply(records[0], self.case.calendar[1])
        book.plan(TargetSet('test', frame.at, {'000001': .5}), 'test')
        book.apply(records[1], self.case.calendar[2])

        fill = book.result().fills[-1]
        self.assertEqual((fill.status, fill.gross_amount), ('filled', 1000.0))
        self.assertGreater(fill.commission, 0)
        self.assertEqual(book._research.pending_symbols, set())

    def _research_pending_case(self, *, b_first='missing', factors=(1, 1, 1),
                               adjusted_prices=(10, 10, 12), limits=None):
        from tests.fee_fixture import execution, fees
        config = execution()
        config['tax'] = {'model': 'none', 'params': {}}
        config['price']['params']['rate'] = '0'
        book = Settlement(D('100000'), fees(), tax_policy(), limits=limits, execution=config,
                          accounting='research')
        records = []
        for index, record in enumerate(self.case.inputs(exit_market=False)[:3]):
            factor = factors[index]
            raw = adjusted_prices[index] / factor
            bars = {'000001': replace(record.bars['000001'], open=raw, high=raw + 1,
                                      low=raw - 1, close=raw)}
            if b_first == 'partial' and index in (1, 2):
                bars['000002'] = replace(record.bars['000002'],
                                         volume=100 if index == 1 else 6000000)
            frame = replace(record.frame, context={**record.frame.context,
                'back_factors': {'000001': str(factor), '000002': '1'}})
            record = replace(record, frame=frame, bars=bars,
                previous_closes={'000001': raw, '000002': 10},
                opening_marks={'000001': D(str(raw)), '000002': D(10)},
                closing_marks={'000001': D(str(raw)), '000002': D(10)},
                opening_amount20={'000001': D(60000000), '000002': D(60000000)},
                closing_amount20={'000001': D(60000000), '000002': D(60000000)},
                opening_volume20={'000001': D(6000000), '000002': D(6000000)},
                opening_eligible=None, registrations=())
            records.append(record)
        return book, tuple(records)

    def test_research_risk_snapshot_includes_completed_nonpending_holding(self):
        book, records = self._research_pending_case(
            limits=AllocationLimits(stock_weight=D('.21')))
        frame, _ = book.apply(records[0], self.case.calendar[1])
        book.plan(TargetSet('pending', frame.at,
                            {'000001': .2, '000002': .2}), 'pending')
        frame, _ = book.apply(records[1], self.case.calendar[2])
        first_day = [fill for fill in book.result().fills
                     if fill.trade_date == self.case.calendar[1]]
        self.assertEqual([(fill.symbol, fill.status) for fill in first_day], [
            ('000001', 'filled'), ('000002', 'rejected')])
        self.assertLess(book._research.cash, D(80000))
        book.plan(None, 'pending')

        source = self.case.inputs(exit_market=False)[2]
        records = list(records)
        records[2] = replace(records[2], bars={**records[2].bars,
                                                '000002': source.bars['000002']})
        book.apply(records[2], self.case.calendar[3])

        retry = [fill for fill in book.result().fills
                 if fill.trade_date == self.case.calendar[2]]
        self.assertEqual([(fill.symbol, fill.status, fill.reason) for fill in retry], [
            ('000002', 'filled', None)])
        self.assertIn('000001', book._research.units)
        self.assertIn('000002', book._research.units)

    def _start_research_pending(self, *, b_first='missing', factors=(1, 1, 1),
                                adjusted_prices=(10, 10, 12), replacement=None):
        book, records = self._research_pending_case(
            b_first=b_first, factors=factors, adjusted_prices=adjusted_prices)
        frame, _ = book.apply(records[0], self.case.calendar[1])
        book.plan(TargetSet('pending', frame.at, {'000001': .2, '000002': .2}), 'pending')
        frame, _ = book.apply(records[1], self.case.calendar[2])
        if replacement is None:
            book.plan(None, 'pending')
        else:
            book.plan(TargetSet('replacement', frame.at, replacement), 'replacement')
        return book, records

    def test_research_retry_does_not_rebalance_completed_symbol(self):
        book, records = self._start_research_pending()
        first = [fill for fill in book.result().fills if fill.trade_date == self.case.calendar[1]]
        self.assertEqual([(f.symbol, f.side, f.status) for f in first], [
            ('000001', Side.BUY, 'filled'), ('000002', Side.BUY, 'rejected')])
        shares_before = book._research.units['000001']

        book.apply(records[2], self.case.calendar[3])
        result = book.result()
        self.assertEqual([order.symbol for order in result.allocations[-1].orders], ['000002'])
        self.assertEqual(book._research.units['000001'], shares_before)
        self.assertEqual([(f.symbol, f.reason) for f in result.fills
                          if f.trade_date == self.case.calendar[2]],
                         [('000002', 'no_tradable_bar')])

    def test_research_partial_fill_retries_only_that_symbol(self):
        book, records = self._start_research_pending(b_first='partial')
        first = [fill for fill in book.result().fills if fill.trade_date == self.case.calendar[1]]
        self.assertEqual([(f.symbol, f.status, f.shares) for f in first], [
            ('000001', 'filled', 2000), ('000002', 'filled', 100)])

        book.apply(records[2], self.case.calendar[3])
        result = book.result()
        self.assertEqual([order.symbol for order in result.allocations[-1].orders], ['000002'])
        self.assertTrue(any(f.symbol == '000002' and f.status == 'filled'
                            for f in result.fills if f.trade_date == self.case.calendar[2]))

    def test_research_explicit_target_replaces_pending_symbols(self):
        book, records = self._start_research_pending(replacement={'000001': .25})
        book.apply(records[2], self.case.calendar[3])
        result = book.result()
        self.assertEqual([order.symbol for order in result.allocations[-1].orders], ['000001'])
        self.assertNotIn('000002', [order.symbol for order in result.allocations[-1].orders])

    def test_research_factor_change_preserves_value_without_completed_symbol_trade(self):
        book, records = self._start_research_pending(
            factors=(1, 1, 2), adjusted_prices=(10, 10, 10))
        equity_before = book.result().equity[-1].equity
        adjusted_units_before = book._research.units['000001']

        book.apply(records[2], self.case.calendar[3])
        result = book.result()
        self.assertEqual([order.symbol for order in result.allocations[-1].orders], ['000002'])
        self.assertEqual(book._research.units['000001'], adjusted_units_before)
        raw_shares_after = int(book._research.units['000001'] * book._research.factors['000001'])
        self.assertEqual(raw_shares_after, 4000)
        self.assertEqual(result.equity[-1].equity, equity_before)

    def test_research_retries_unfinished_target_and_new_target_replaces_it(self):
        from tests.fee_fixture import execution, fees
        for partial, cancel in ((False,False),(True,False),(False,True)):
            with self.subTest(partial=partial,cancel=cancel):
                config=execution(); config['tax']['model']='none'
                book=Settlement(D('100000'),fees(),tax_policy(),execution=config,accounting='research')
                case=SettlementFixture()
                for index,record in enumerate(case.inputs(exit_market=False)):
                    bar=replace(record.bars['000001'],volume=100 if partial and index==1 else 6000000)
                    record=replace(record,
                        frame=replace(record.frame,context={**record.frame.context,'back_factors':{'000001':'1'}}),
                        bars={'000001':bar},opening_marks={'000001':D(10)},closing_marks={'000001':D(10)},
                        opening_eligible=() if not partial and index==1 else ('000001',),registrations=())
                    frame,_=book.apply(record,case.calendar[index+1])
                    weights={'000001':.2} if index==0 else {} if cancel and index==1 else None
                    book.plan(None if weights is None else TargetSet('test',frame.at,weights),'test')
                result=book.result()
                later=[fill for fill in result.fills if fill.trade_date>case.calendar[1] and fill.status=='filled']
                if cancel:
                    self.assertEqual(later,[])
                    self.assertEqual(result.holdings[-1][1],())
                else:
                    self.assertTrue(later)
                    self.assertEqual(later[0].trade_date,case.calendar[2])
                    self.assertGreater(result.holdings[-1][1][0].shares,D(100))

    def test_research_retries_exit_after_trading_resumes(self):
        from tests.fee_fixture import execution, fees
        config=execution(); config['tax']['model']='none'
        book=Settlement(D('100000'),fees(),tax_policy(),execution=config,accounting='research')
        case=SettlementFixture()
        for index,record in enumerate(case.inputs(exit_market=False)):
            record=replace(record,
                frame=replace(record.frame,context={**record.frame.context,'back_factors':{'000001':'1'}}),
                bars={'000001':record.bars['000001']},opening_marks={'000001':D(10)},
                closing_marks={'000001':D(10)},opening_eligible=() if index==2 else ('000001',),registrations=())
            frame,_=book.apply(record,case.calendar[index+1])
            weights={'000001':.2} if index==0 else {} if index==1 else None
            book.plan(None if weights is None else TargetSet('test',frame.at,weights),'test')
        result=book.result()
        self.assertEqual([fill.status for fill in result.fills],['filled','rejected','filled'])
        self.assertEqual(result.fills[-1].side,Side.SELL)
        self.assertEqual(result.fills[-1].trade_date,case.calendar[3])
        self.assertEqual(result.holdings[-1][1],())

    def test_research_partial_sale_then_exit_does_not_create_rounding_positions(self):
        from tests.fee_fixture import execution,fees
        for factor in ('5.5813831068165','3.1','7','1.00137'):
            with self.subTest(factor=factor):
                config=execution();config['tax']['model']='none';config['price']['params']['rate']='0'
                settlement=Settlement(D('100000'),fees(),tax_policy(),execution=config,accounting='research')
                case=SettlementFixture()
                for index,record in enumerate(case.inputs(exit_market=False)):
                    frame=replace(record.frame,context={**record.frame.context,'back_factors':{'000001':factor}})
                    bars={'000001':replace(record.bars['000001'],open=10,high=11,low=9,close=10)}
                    record=replace(record,frame=frame,bars=bars,opening_marks={'000001':D(10)},
                                   closing_marks={'000001':D(10)},registrations=())
                    frame,_=settlement.apply(record,case.calendar[index+1])
                    weights=({'000001':.02},{'000001':.015},{},None)[index]
                    settlement.plan(None if weights is None else TargetSet('test',frame.at,weights),'test')
                result=settlement.result()
                self.assertEqual([fill.shares for fill in result.fills],[200,100,100])
                self.assertEqual(result.holdings[-1][1],())
                self.assertEqual(result.equity[-1].cash,D('99983'))

    def test_research_uses_adjusted_units_and_raw_execution_without_distribution(self):
        from tests.fee_fixture import execution,fees
        config=execution();config['tax']={'model':'none','params':{}}
        config['price']['params']['rate']='0'
        settlement=Settlement(D('100000'),fees(),tax_policy(),execution=config,accounting='research')
        records=list(SettlementFixture().inputs(exit_market=False))
        points=[]
        for index,record in enumerate(records):
            factor=D(1) if index<2 else D(2)
            bars={'000001':replace(record.bars['000001'],open=float(10/factor),
                high=float(11/factor),low=float(9/factor),close=float(10/factor))}
            frame=replace(record.frame,fields={'000001':{'close':float(10/factor),'signal_close':10.}},
                          context={**record.frame.context,'back_factors':{'000001':str(factor)}})
            record=replace(record,frame=frame,bars=bars,opening_marks={'000001':D(10)/factor},
                closing_marks={'000001':D(10)/factor},registrations=())
            frame,account=settlement.apply(record,SettlementFixture.calendar[index+1])
            self.assertTrue(frame.fields)
            points.append(account.equity)
            settlement.plan(TargetSet('test',frame.at,{'000001':.2}) if index==0 else None,'test')
        result=settlement.result()
        self.assertEqual(points[1],points[2])
        self.assertEqual(points[2],points[3])
        self.assertLess(points[1],D('100000'))
        self.assertEqual(len(result.fills),1)
        self.assertEqual(result.fills[0].price,10)
        self.assertEqual(result.distribution_postings,())
        self.assertEqual(result.tax_payments,())
        self.assertTrue(all(not frame.fields and not frame.history for frame in result.frames))

    def setUp(self):
        self.case = SettlementFixture()

    def test_pretax_listing_credit_preserves_equity_before_tradability(self):
        from tests.fee_fixture import execution, fees
        from plugins.strategies.breadth_low_amount_reversal import Params, Strategy
        results=[]
        event=normalize_dividend_records([dict(code='000001',registration_date='2020-02-03',
            operation_date='2020-02-04',payment_date='2020-02-05',stock_market_date='2020-02-05',
            cash_per_share='.3',stock_dividend_per_share='.1',reserve_to_stock_per_share=0)])[0]
        for credit in (date(2020,2,4),date(2020,2,5)):
            config=execution();config['tax']={'model':'none','params':{}}
            settlement=Settlement(D('100000'),fees(),tax_policy(),limits=AllocationLimits(),execution=config)
            strategy=Strategy(Params(breadth_window=1))
            records=list(self.case.inputs(exit_market=False))
            records[1]=replace(records[1],registrations=(RegistrationAction('plan',event,'a'*64,
                credit,None,None,None),))
            for index,record in enumerate(records):
                frame,account=settlement.apply(record,self.case.calendar[index+1])
                settlement.plan(strategy.generate(frame,account),strategy.strategy_id)
            results.append(settlement.result())
        self.assertEqual([p.equity for p in results[0].equity],[p.equity for p in results[1].equity])
        self.assertEqual(results[0].fills,results[1].fills)
        self.assertEqual(results[0].holdings[-1],results[1].holdings[-1])
        self.assertEqual(results[1].equity[2].receivables,D('1560'))
        self.assertEqual(results[1].equity[3].receivables,D(0))

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
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0,
            'code': '000001', 'registration_date': '2020-02-03', 'operation_date': '2020-02-04',
            'payment_date': '2020-02-05', 'stock_market_date': '2020-02-06',
            'cash_per_share': '0.3', 'stock_dividend_per_share': '0.1',
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

        from backend import save_failed_backtest
        from protocol import BacktestError, BacktestRequest
        from market import build_data

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
            value = save_failed_backtest(build_data(Path(root)), request.run_id, error, partial)
            self.assertEqual(value, 'data/result/failed-trace/failed.json')
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
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0, 'code': 'A', 'registration_date': '2020-01-10', 'operation_date': '2020-01-13', 'payment_date': '2020-01-20', 'cash_per_share': '1'}])[0]
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
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'code': 'A', 'registration_date': '2020-07-02', 'operation_date': '2020-07-03', 'payment_date': '2020-07-04', 'stock_market_date': '2020-07-06', 'cash_per_share': '.3', 'stock_dividend_per_share': '.1'}])[0]
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
