from datetime import date
from decimal import Decimal as D
import unittest
from types import SimpleNamespace
import core.calculation as calculation

from core.calculation import FeePolicy, TaxPolicy, TradeRules
from protocol import Bar, Fill, Order, Side


def fee_policy(rounding='none'):
    return FeePolicy.from_mapping({
        'commission_rate': '.00025', 'minimum_commission': '5',
        'stamp_rates': [{'effective': '2020-01-01', 'rate': '.001'}],
        'transfer_rates': [{'effective': '2020-01-01', 'rate': '.00002'}],
        'rounding': rounding,
    }, 'a' * 64)


def tax_policy():
    return TaxPolicy.from_mapping({
        'effective': '2015-09-09', 'policy': '财税2015-101',
        'payment_timing': 'later_of_disposal_and_income',
        'liability': 'current_holding_period', 'rounding': 'none',
    }, 'b' * 64)


def rules(price, quantity):
    return TradeRules({
        'price': price,
        'quantity': quantity,
        'fees': {'model': 'dated', 'params': {'rounding': 'none'}},
        'tax': {'model': 'fifo', 'params': {}},
        'check': {'model': 'cn_a_share', 'params': {'tick': '.01', 'min_qty': 100, 'step': 100}},
    }, fee_policy(), tax_policy())


class TradeRulesTests(unittest.TestCase):
    def test_old_execution_models_are_removed(self):
        self.assertFalse(hasattr(calculation, 'ExecutionEngine'))
        self.assertFalse(hasattr(calculation, 'PositionBook'))

    def test_rules_expose_no_unused_audit_state(self):
        value = rules({'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
                      {'model': 'capacity', 'params': {}})
        self.assertFalse(hasattr(value, '_provenance'))

    def setUp(self):
        self.order = Order('o', 'A', Side.BUY, 1000, date(2024, 1, 2),
                           date(2024, 1, 3), 'entry')
        self.bar = Bar('A', date(2024, 1, 3), 10, 10, 10, 10, 10000, 100000)

    def test_price_models_choose_only_the_price_action(self):
        ratio = rules({'model': 'ratio', 'params': {'rate': '0.001', 'tick': '0.01'}},
                      {'model': 'capacity', 'params': {}})
        tick = rules({'model': 'tick', 'params': {'ticks': 2, 'tick': '0.01'}},
                     {'model': 'capacity', 'params': {}})
        self.assertEqual(ratio.price(self.order, self.bar, 100, None), D('10.01'))
        self.assertEqual(tick.price(self.order, self.bar, 100, None), D('10.02'))

    def test_protocol_requires_explicit_ratio_and_capacity_fields(self):
        with self.assertRaises(ValueError):
            TradeRules({
            'price': {'model': 'ratio', 'params': {}},
            'quantity': {'model': 'capacity', 'params': {}},
            'fees': {'model': 'dated', 'params': {'rounding': 'none'}},
            'tax': {'model': 'fifo', 'params': {}},
            'check': {'model': 'cn_a_share', 'params': {'tick': '.01', 'min_qty': 100, 'step': 100}},
            }, fee_policy(), tax_policy())

    def test_volume_share_price_and_participation_use_prior20_volume(self):
        value = rules(
            {'model': 'volume_share', 'params': {'impact': '0.1', 'tick': '0.01', 'capacity_basis': 'prior20'}},
            {'model': 'participation', 'params': {'participation': '0.2', 'capacity_basis': 'prior20'}},
        )
        self.assertEqual(value.quantity(self.order, self.bar, price=D('10'), cash=D('100000'),
                                        positions={}, sellable={}, reserve=D('0'),
                                     volume20=D('2000')), 400)
        self.assertEqual(value.price(self.order, self.bar, 400, None,
                                     volume20=D('2000')), D('10.04'))
        with self.assertRaisesRegex(ValueError, 'prior20'):
            value.quantity(self.order, self.bar, price=D('10'), cash=D('100000'),
                           positions={}, sellable={}, reserve=D('0'), volume20=None)

    def test_volume_share_is_quadratic(self):
        value = rules(
            {'model': 'volume_share', 'params': {'impact': '0.1', 'tick': '0.01', 'capacity_basis': 'prior20'}},
            {'model': 'capacity', 'params': {}},
        )
        self.assertEqual(value.price(self.order, self.bar, 100, None, volume20=D('1000')), D('10.01'))
        self.assertEqual(value.price(self.order, self.bar, 200, None, volume20=D('1000')), D('10.04'))

    def test_volume_joint_quantity_does_not_depend_on_previous_close(self):
        value = rules(
            {'model': 'volume_share', 'params': {'impact': '.1', 'tick': '.01', 'capacity_basis': 'prior20'}},
            {'model': 'capacity', 'params': {}},
        )
        kwargs = dict(price=D('10'), cash=D('2170'), positions={}, sellable={},
                      reserve=D('0'), volume20=D('2000'))
        order = Order('joint', 'A', Side.BUY, 217, date(2024, 1, 2), date(2024, 1, 3), 'entry')
        with_previous = value.quantity(order, self.bar, previous_close=D('10'), **kwargs)
        without_previous = value.quantity(order, self.bar, previous_close=None, **kwargs)
        self.assertEqual(with_previous, 200)
        self.assertEqual(without_previous, with_previous)

    def test_sell_odd_balance_preserves_legal_partial_remainder(self):
        value = rules(
            {'model': 'ratio', 'params': {'rate': '0', 'tick': '.01'}},
            {'model': 'capacity', 'params': {}},
        )
        for requested, expected in ((50, 50), (30, 0), (120, 100), (150, 150)):
            order = Order(f'sell-{requested}', 'A', Side.SELL, requested,
                          date(2024, 1, 2), date(2024, 1, 3), 'exit')
            self.assertEqual(value.quantity(order, self.bar, price=D('10'), cash=D('0'),
                                           positions={'A': 150}, sellable={'A': 150},
                                           reserve=D('0')), expected)
            if expected:
                self.assertIsNone(value.check(order, self.bar, None, price=D('10'),
                                              quantity=expected, positions={'A': 150},
                                              sellable={'A': 150}))
            elif requested == 30:
                self.assertEqual(value.check(order, self.bar, None, price=D('10'),
                                             quantity=requested, positions={'A': 150},
                                             sellable={'A': 150}), 'quantity_step_invalid')

    def test_dataset_check_skips_warmup_and_requires_prior20_only_when_selected(self):
        before = SimpleNamespace(trade_date=date(2024, 1, 2), bars={'A': self.bar},
                                 opening_volume20={})
        current = SimpleNamespace(trade_date=date(2024, 1, 3), bars={'A': self.bar},
                                  opening_volume20={})
        dataset = SimpleNamespace(records=(before, current))
        volume = rules(
            {'model': 'volume_share', 'params': {'impact': '.1', 'tick': '.01', 'capacity_basis': 'prior20'}},
            {'model': 'capacity', 'params': {}},
        )
        with self.assertRaisesRegex(ValueError, 'opening_volume20'):
            volume.check(phase='dataset', dataset=dataset, start=date(2024, 1, 3))
        rules({'model': 'ratio', 'params': {'rate': '0', 'tick': '.01'}},
              {'model': 'capacity', 'params': {}}).check(
                  phase='dataset', dataset=dataset, start=date(2024, 1, 3))

    def test_cash_capacity_charges_minimum_fee_once(self):
        value = rules(
            {'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
            {'model': 'capacity', 'params': {}},
        )
        order = Order('capacity', 'A', Side.BUY, 200, date(2024, 1, 2), date(2024, 1, 3), 'entry')
        quantity = value.quantity(order, self.bar, price=D('10'), cash=D('2005.04'),
                                  positions={}, sellable={}, reserve=D('0'))
        self.assertEqual(quantity, 200)

    def test_volume_capacity_searches_the_minimum_based_grid(self):
        value = TradeRules({
            'price': {'model': 'volume_share', 'params': {'impact': '.1', 'tick': '.01', 'capacity_basis': 'prior20'}},
            'quantity': {'model': 'capacity', 'params': {}},
            'fees': {'model': 'dated', 'params': {'rounding': 'none'}},
            'tax': {'model': 'fifo', 'params': {}},
            'check': {'model': 'cn_a_share', 'params': {'tick': '.01', 'min_qty': 200, 'step': 3}},
        }, fee_policy(), tax_policy())
        order = Order('grid', 'A', Side.BUY, 217, date(2024, 1, 2), date(2024, 1, 3), 'entry')
        price = value.price(order, self.bar, 0, D('10'), volume20=D('2000'))
        self.assertEqual(value.quantity(order, self.bar, price=price, previous_close=D('10'),
                                        cash=D('2170'), positions={}, sellable={}, reserve=D('0'),
                                        volume20=D('2000')), 215)

    def test_fees_support_none_and_half_up_rounding(self):
        none = rules({'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
                     {'model': 'capacity', 'params': {}})
        self.assertEqual(none.fees(D('123.456'), Side.SELL, date(2024, 1, 3)).transfer_fee,
                         D('0.00246912'))
        rounded = TradeRules({
            'price': {'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
            'quantity': {'model': 'capacity', 'params': {}},
            'fees': {'model': 'dated', 'params': {'rounding': 'half_up'}},
            'tax': {'model': 'fifo', 'params': {}},
            'check': {'model': 'cn_a_share', 'params': {'tick': '.01', 'min_qty': 100, 'step': 100}},
        }, fee_policy('half_up'), tax_policy())
        self.assertEqual(rounded.fees(D('123.456'), Side.SELL, date(2024, 1, 3)).transfer_fee,
                         D('0.00'))

    def test_check_rejects_missing_rules_without_symbol_special_cases(self):
        value = rules({'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
                     {'model': 'capacity', 'params': {}})
        order = Order('o', 'NOT_A_STOCK_CODE', Side.BUY, 100, date(2024, 1, 2),
                      date(2024, 1, 3), 'entry')
        bar = Bar('NOT_A_STOCK_CODE', date(2024, 1, 3), 10, 10, 10, 10, 100, 1000)
        self.assertIsNone(value.check(order, bar, D('10'), price=D('10'), quantity=100,
                                      positions={}, sellable={}))
        self.assertIsNone(value.check(order, bar, None, price=D('10'), quantity=100,
                                      positions={}, sellable={}))

    def test_tax_model_uses_natural_holding_period(self):
        value = rules({'model': 'ratio', 'params': {'rate': '0', 'tick': '0.01'}},
                      {'model': 'capacity', 'params': {}})
        value.tax(date(2024, 1, 3), phase='begin')
        value.tax(date(2024, 1, 3), (), phase='end')
