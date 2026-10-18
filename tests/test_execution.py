from datetime import date
from decimal import Decimal as D
import unittest

from core.calculation import (AccountExecutionSession, AccountLedger, AllocationLimits,
                              OpeningRiskSnapshot, TradeRules)
from tests.fee_fixture import fees, tax_policy
from protocol import Bar, Fill, Order, Side


def rules(price=None, quantity=None):
    return TradeRules({
        'price': price or {'model': 'ratio', 'params': {'rate': '.001', 'tick': '.01'}},
        'quantity': quantity or {'model': 'capacity', 'params': {}},
        'fees': {'model': 'dated', 'params': {'rounding': 'none'}},
        'tax': {'model': 'fifo', 'params': {}},
        'check': {'model': 'cn_a_share', 'params': {'tick': '.01', 'min_qty': 100, 'step': 100}},
    }, fees(), tax_policy())


class TradeExecutionTests(unittest.TestCase):
    def order(self, side=Side.BUY, shares=100):
        return Order('o', 'A', side, shares, date(2024, 1, 2), date(2024, 1, 3), 'test')

    def bar(self, volume=10000):
        return Bar('A', date(2024, 1, 3), 10, 10, 10, 10, volume, 100000)

    def test_buy_uses_price_quantity_and_all_fees(self):
        account = AccountLedger(D('100000'))
        session = AccountExecutionSession(account, rules())
        session.submit(self.order(shares=100))
        fill = session.execute('o', self.bar(), previous_close=10)
        self.assertEqual(fill.status, 'filled')
        self.assertEqual(fill.price, 10.01)
        self.assertEqual(account.cash, D('98994'))

    def test_sell_respects_t_plus_one(self):
        account = AccountLedger(D('100000'))
        session = AccountExecutionSession(account, rules())
        session.submit(self.order(shares=100))
        session.execute('o', self.bar(), previous_close=10)
        session.submit(Order('s', 'A', Side.SELL, 100, date(2024, 1, 2), date(2024, 1, 3), 'exit'))
        reason = session.rules.check(self.order(Side.SELL, 100), self.bar(), 10,
                                     price=D('10'), quantity=100, positions={}, sellable={})
        self.assertEqual(reason, 't_plus_one_locked')

    def test_volume_share_requires_prior20_reference(self):
        value = rules(
            {'model': 'volume_share', 'params': {'impact': '.1', 'tick': '.01', 'capacity_basis': 'prior20'}},
            {'model': 'participation', 'params': {'participation': '.2', 'capacity_basis': 'prior20'}},
        )
        with self.assertRaisesRegex(ValueError, 'prior20'):
            value.price(self.order(), self.bar(), 100, D('10'))

    def test_participation_capacity_is_shared_and_replay_is_idempotent(self):
        value = rules(
            {'model': 'ratio', 'params': {'rate': '0', 'tick': '.01'}},
            {'model': 'participation', 'params': {'participation': '.2', 'capacity_basis': 'prior20'}},
        )
        account = AccountLedger(D('100000'))
        session = AccountExecutionSession(account, value)
        first = self.order(shares=150)
        second = Order('second', 'A', Side.BUY, 150, date(2024, 1, 2), date(2024, 1, 3), 'test')
        session.submit(first); session.submit(second)
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {'A': D('10')}, {'A': D('100000')},
                                   D('0'), D('0'), AllocationLimits())
        bars = {'A': self.bar()}
        fills = session.execute_open(date(2024, 1, 3), bars, {'A': 10}, risk=risk,
                                     volume20={'A': D('1000')})
        self.assertEqual([fill.shares for fill in fills], [100, 100])
        replay = session.execute_open(date(2024, 1, 3), bars, {'A': 10}, risk=risk,
                                     volume20={'A': D('1000')})
        self.assertEqual(replay, fills)
        with self.assertRaises(ValueError):
            session.execute_open(date(2024, 1, 3), bars, {'A': 10}, risk=risk,
                                 volume20={'A': D('1000')}, cash_reserve=D('1'))

    def test_opening_market_exclusion_cancels_pending_buy_before_fill(self):
        account = AccountLedger(D('100000'))
        session = AccountExecutionSession(account, rules())
        session.submit(self.order(shares=100))
        day = date(2024, 1, 3)
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {'A': D('10')},
                                   {'A': D('100000')}, D('0'), D('0'))

        fills = session.execute_open(day, {'A': self.bar()}, {'A': 10}, risk=risk,
                                     eligible=())

        self.assertEqual(fills, ())
        self.assertEqual(session.pending, ())
        self.assertEqual(account.order_cancellations[0].reason, 'market_ineligible')
        self.assertEqual(account.cash, D('100000'))
        replay = session.execute_open(day, {'A': self.bar()}, {'A': 10}, risk=risk,
                                     eligible=())
        self.assertEqual(replay, fills)
        self.assertEqual(len(account.order_cancellations), 1)

    def test_invalid_opening_risk_does_not_cancel_ineligible_buy(self):
        account = AccountLedger(D('100000'))
        session = AccountExecutionSession(account, rules())
        session.submit(self.order(shares=100))
        day = date(2024, 1, 3)
        risk = OpeningRiskSnapshot(day, {'A': D('10')}, {'A': D('100000')}, D('0'), D('0'))

        with self.assertRaisesRegex(ValueError, 'liquidity must be observed before execution day'):
            session.execute_open(day, {'A': self.bar()}, {'A': 10}, risk=risk, eligible=())

        self.assertEqual(len(session.pending), 1)
        self.assertEqual(account.order_cancellations, ())

    def test_opening_reserve_rejects_callable_and_is_part_of_replay_inputs(self):
        account = AccountLedger(D('100000'))
        value = rules()
        session = AccountExecutionSession(account, value)
        order = self.order(shares=100)
        session.submit(order)
        day = date(2024, 1, 3)
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {'A': D('10')},
                                   {'A': D('100000')}, D('0'), D('0'))
        with self.assertRaises(TypeError):
            session.execute_open(day, {'A': self.bar()}, {'A': 10}, risk=risk,
                                 cash_reserve=lambda: D('1'))

    def test_tax_preview_failure_rolls_back_tax_and_account_state(self):
        from core.calculation import TaxHolding, TaxRight
        account = AccountLedger(D('1000'))
        value = rules()
        session = AccountExecutionSession(account, value)
        buy = Order('seed', 'A', Side.BUY, 100, date(2024, 1, 1), date(2024, 1, 2), 'entry')
        account.register_order(buy)
        account.record_fill('seed', Fill('seed', 'A', Side.BUY, date(2024, 1, 2),
                                         100, 10, 1000, 0, 0, 'filled'))
        value._tax_ledger.lots = (TaxHolding(
            'lot', 'A', date(2024, 1, 2), 100,
            (TaxRight(date(2024, 1, 2), 'div', 'cash', date(2024, 1, 2), date(2024, 1, 5), D('1'), 'a' * 64),)),)
        day = date(2024, 1, 3)
        value.tax(day, phase='begin')
        sell = self.order(side=Side.SELL, shares=100)
        session.submit(sell)
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {'A': D('10')},
                                   {'A': D('100000')}, D('0'), D('0'))
        before = (account.cash, account.lots, value._tax_snapshot(), session.fills)
        with self.assertRaisesRegex(RuntimeError, 'callback'):
            session.execute_open(day, {'A': self.bar()}, {'A': 10}, risk=risk,
                                 on_fill=lambda _fill: (_ for _ in ()).throw(RuntimeError('callback')))
        self.assertEqual((account.cash, account.lots, value._tax_snapshot(), session.fills), before)

    def test_opening_risk_counts_transfer_fee_in_post_equity(self):
        from protocol import Fill
        fill = Fill('o', 'A', Side.BUY, date(2024, 1, 3), 10, 10, 100, 0, 0,
                    'filled', transfer_fee=100)
        snapshot = OpeningRiskSnapshot(date(2024, 1, 2), {'A': D('10')},
                                        {'A': D('100000')}, D('0'), D('0'),
                                        AllocationLimits(stock_weight=D('.99'), adv_fraction=D('1')))
        fill_order = self.order(shares=10)
        self.assertEqual(rules().check(phase='opening_rejection', order=fill_order, fill=fill,
                                       equity=D('100'), holdings={}, snapshot=snapshot),
                         'position_above_stock_limit')
