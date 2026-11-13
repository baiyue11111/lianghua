from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.kernel import Kernel
from plugins.data import Settlement
from protocol import AccountSnapshot, MarketFrame, Side, TargetSet
from tests.fee_fixture import fees, tax_policy


DAYS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))


def timestamp(day):
    return datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc)


def snapshot(day, cash='6000', equity='10000', positions=None, sellable=None):
    return AccountSnapshot(timestamp(day), D(cash), D(equity),
                           {'A': D('400')} if positions is None else positions,
                           {'A': D('400')} if sellable is None else sellable)


class Strategy:
    strategy_id = 'example'

    def __init__(self, weights):
        self.weights = weights
        self.days = []

    def generate(self, frame, account):
        self.days.append((frame, account))
        if self.weights is None:
            return None
        return TargetSet(self.strategy_id, frame.at, self.weights)


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('core.kernel.datetime', wraps=datetime).start()
        self.addCleanup(patch.stopall)
        self.clock.now.return_value = datetime(2024, 1, 3, 23, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.strategy = Strategy({'B': 0.5})
        self.records = tuple(SimpleNamespace(
            trade_date=day,
            frame=MarketFrame(timestamp(day), 'test', {}, {}, {}),
            closing_marks={'A': D('10'), 'B': D('10')},
            closing_amount20={'A': D('1000000'), 'B': D('1000000')},
        ) for day in DAYS[:2])
        self.dataset = SimpleNamespace(records=self.records, calendar=DAYS,
            policies={'fees': fees(), 'tax_policy': tax_policy()}, provenance={'test': 'evidence'})
        self.req = SimpleNamespace(start=timestamp(DAYS[0]), end=timestamp(DAYS[1]),
                                   as_of=DAYS[1], settlement_parameters={})
        self.kernel = Kernel(None, None, None, Settlement)
        self.kernel.prepare = lambda request: SimpleNamespace(
            request=request, strategy=self.strategy, dataset=self.dataset,
            effective_config={'strategy': 'example'}, config_hash='hash')
        self.accounts = {day: snapshot(day) for day in DAYS[:2]}

    def test_real_daily_account_drives_sell_and_buy_without_fills(self):
        result = self.kernel.predict(self.req, self.accounts, pending=())
        self.assertEqual([(o.symbol, o.side, o.requested_shares) for o in result.allocation.orders],
                         [('A', Side.SELL, 400), ('B', Side.BUY, 500)])
        self.assertEqual(result.signal_date, DAYS[1])
        self.assertEqual(result.next_session, DAYS[2])
        self.assertEqual(result.status, 'reference_orders')
        self.assertEqual(result.provenance, {'test': 'evidence'})
        self.assertEqual(result.prices, {'A': D('10'), 'B': D('10')})
        self.assertEqual(result.buy_value, D('5000'))
        self.assertEqual(result.cash_gap, D('0'))
        self.assertEqual([a.as_of for _, a in self.strategy.days],
                         [timestamp(DAYS[0]), timestamp(DAYS[1])])
        self.assertEqual(self.strategy.days[-1][0].context['next_session'], '2024-01-04')
        self.assertEqual(self.accounts[DAYS[1]].cash, D('6000'))

    def test_none_means_no_rebalance_and_empty_target_means_sell(self):
        self.strategy.weights = None
        result = self.kernel.predict(self.req, self.accounts, pending=())
        self.assertIsNone(result.target)
        self.assertEqual(result.allocation.orders, ())
        self.strategy = Strategy({})
        result = self.kernel.predict(self.req, self.accounts, pending=())
        self.assertEqual([(o.symbol, o.side, o.requested_shares) for o in result.allocation.orders],
                         [('A', Side.SELL, 400)])

    def test_rejects_missing_or_wrong_day_account_history(self):
        accounts = {DAYS[1]: self.accounts[DAYS[1]], DAYS[2]: snapshot(DAYS[2])}
        with self.assertRaises(ValueError) as caught:
            self.kernel.predict(self.req, accounts, pending=())
        self.assertIn('2024-01-02', str(caught.exception))
        self.assertIn('2024-01-04', str(caught.exception))
        self.assertNotIn('6000', str(caught.exception))
        accounts = dict(self.accounts)
        accounts[DAYS[0]] = snapshot(DAYS[1])
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, accounts, pending=())

    def test_rejects_stale_data_and_pending_orders(self):
        self.req.end = timestamp(DAYS[2])
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, self.accounts, pending=())
        self.req.end = timestamp(DAYS[1])
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, self.accounts, pending=('unfilled',))

    def test_rejects_invalid_account_and_unavailable_sell(self):
        for bad in (
            snapshot(DAYS[1], cash='-1'),
            snapshot(DAYS[1], equity='0'),
            snapshot(DAYS[1], positions={'A': D('400.5')}),
            snapshot(DAYS[1], sellable={'A': D('401')}),
            snapshot(DAYS[1], sellable={}),
            snapshot(DAYS[1], sellable={'A': D('200')}),
        ):
            with self.subTest(bad=bad):
                accounts = {**self.accounts, DAYS[1]: bad}
                with self.assertRaises(ValueError):
                    self.kernel.predict(self.req, accounts, pending=())

    def test_rejects_missing_price_for_real_holding(self):
        self.records[-1].closing_marks.pop('A')
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, self.accounts, pending=())

    def test_rejects_a_close_that_has_not_happened(self):
        future = date(2999, 1, 2)
        following = date(2999, 1, 3)
        record = SimpleNamespace(trade_date=future,
            frame=MarketFrame(timestamp(future), 'test', {}, {}, {}),
            closing_marks={'A': D('10')}, closing_amount20={'A': D('1000000')})
        self.dataset.records = (record,)
        self.dataset.calendar = (future, following)
        self.req.start = self.req.end = timestamp(future)
        self.req.as_of = future
        with self.assertRaisesRegex(ValueError, 'future'):
            self.kernel.predict(self.req, {future: snapshot(future)}, pending=())

    def test_rejects_research_accounting(self):
        self.req.settlement_parameters = {'accounting': 'research'}
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, self.accounts, pending=())

    def test_rejects_stale_or_opened_session(self):
        self.clock.now.return_value = datetime(2024, 1, 4, 9, 24, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.kernel.predict(self.req, self.accounts, pending=())
        for now in (datetime(2024, 1, 4, 9, 25, tzinfo=ZoneInfo('Asia/Shanghai')),
                    datetime(2024, 1, 5, 8, tzinfo=ZoneInfo('Asia/Shanghai'))):
            self.clock.now.return_value = now
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.kernel.predict(self.req, self.accounts, pending=())

    def test_rejects_when_calculation_crosses_opening_auction(self):
        self.clock.now.side_effect = [
            datetime(2024, 1, 4, 9, 24, 59, tzinfo=ZoneInfo('Asia/Shanghai')),
            datetime(2024, 1, 4, 9, 25, tzinfo=ZoneInfo('Asia/Shanghai')),
        ]
        with self.assertRaises(ValueError):
            self.kernel.predict(self.req, self.accounts, pending=())

    def test_cash_gap_does_not_assume_sales_or_fees(self):
        accounts = {**self.accounts, DAYS[1]: snapshot(DAYS[1], cash='1000')}
        result = self.kernel.predict(self.req, accounts, pending=())
        self.assertEqual(result.buy_value, D('5000'))
        self.assertEqual(result.cash_gap, D('4000'))

    def test_prediction_uses_the_configured_snapshot_cutoff(self):
        for cutoff in (None, DAYS[1], DAYS[2]):
            self.req.as_of = cutoff
            with self.subTest(cutoff=cutoff):
                self.assertEqual(self.kernel.predict(self.req, self.accounts, pending=()).signal_date, DAYS[1])


if __name__ == '__main__':
    unittest.main()
