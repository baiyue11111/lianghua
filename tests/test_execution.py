from tests.fee_fixture import fees
from datetime import date
from math import nan
import unittest
import execution as execution

from protocal import Bar, Order, Side
from execution import ExecutionEngine, PositionBook


class ExecutionEngineTests(unittest.TestCase):
    def test_partial_fill_retains_only_unfilled_quantity_and_lineage(self):
        book = PositionBook(1500)
        order = Order("partial", "000001", Side.BUY, 200, date(2020, 1, 2), date(2020, 1, 3), "entry")
        bar = Bar("000001", date(2020, 1, 3), 10, 11, 9, 10, 10000, 100000)
        fill = ExecutionEngine(fees()).execute(order, bar, book)
        self.assertEqual(fill.shares, 100)
        pending = execution.remaining_order(order, fill)
        self.assertEqual(pending.requested_shares, 100)
        self.assertEqual(pending.order_id, "partial")
        self.assertEqual(pending.signal_date, date(2020, 1, 2))
        self.assertEqual(pending.reason, "entry")
        rejected = ExecutionEngine(fees()).execute(pending, bar, book)
        self.assertEqual(execution.remaining_order(pending, rejected), pending)
        funded = PositionBook(5000)
        filled = ExecutionEngine(fees()).execute(pending, bar, funded)
        self.assertIsNone(execution.remaining_order(pending, filled))

    def test_invalid_order_or_volume_never_changes_book(self):
        for side, quantity, volume, bar_symbol in (
            (Side.BUY, -100, 10000, "000001"),
            (Side.SELL, -100, 10000, "000001"),
            (Side.BUY, 100, float('nan'), "000001"),
            (Side.BUY, 100, float('inf'), "000001"),
            (Side.BUY, 100, 10000, "000002"),
        ):
            with self.subTest(side=side, quantity=quantity, volume=volume, symbol=bar_symbol):
                book = PositionBook(10000, {"000001": 200}, {"000001": 8.0})
                order = Order("invalid", "000001", side, quantity, date(2020, 1, 2), date(2020, 1, 3), "test")
                bar = Bar(bar_symbol, date(2020, 1, 3), 10, 11, 9, 10, volume, 100000)
                fill = ExecutionEngine(fees()).execute(order, bar, book)
                self.assertEqual(fill.status, "rejected")
                self.assertEqual(fill.shares, 0)
                self.assertEqual(book.cash, 10000)
                self.assertEqual(book.holdings, {"000001": 200})
                self.assertEqual(book.average_costs, {"000001": 8.0})

    def test_same_day_purchases_cannot_be_sold_but_old_shares_can(self):
        engine = ExecutionEngine(fees())
        book = PositionBook(10000, {"000001": 100}, {"000001": 8.0})
        today = date(2020, 1, 3)
        bar = Bar("000001", today, 10, 11, 9, 10, 100000, 1000000)
        for order_id in ("buy-a", "buy-b"):
            engine.execute(Order(order_id, "000001", Side.BUY, 100,
                                 date(2020, 1, 2), today, "entry"), bar, book)
        self.assertEqual(book.shares("000001"), 300)
        sell = Order("sell", "000001", Side.SELL, 300, date(2020, 1, 2), today, "exit")
        fill = engine.execute(sell, bar, book)
        self.assertEqual(fill.shares, 100)
        self.assertEqual(book.shares("000001"), 200)
        cash = book.cash
        self.assertEqual(engine.execute(sell, bar, book).shares, 0)
        self.assertEqual(book.cash, cash)
        later = Bar("000001", date(2020, 1, 6), 10, 11, 9, 10, 100000, 1000000)
        self.assertEqual(engine.execute(sell, later, book).shares, 200)
        self.assertEqual(book.shares("000001"), 0)

    def test_sell_odd_balance_is_not_trapped_or_split(self):
        for held, requested, expected in ((150, 150, 150), (50, 50, 50),
                                           (150, 120, 100), (150, 50, 50),
                                           (150, 30, 0), (250, 150, 150)):
            with self.subTest(held=held, requested=requested):
                book = PositionBook(1000, {"000001": held}, {"000001": 8.0})
                order = Order("odd", "000001", Side.SELL, requested,
                              date(2020, 1, 2), date(2020, 1, 3), "exit")
                bar = Bar("000001", date(2020, 1, 3), 10, 11, 9, 10, 100000, 1000000)
                fill = ExecutionEngine(fees()).execute(order, bar, book)
                self.assertEqual(fill.shares, expected)
                self.assertEqual(book.shares("000001"), held - expected)
                self.assertAlmostEqual(book.cash, 1000 + fill.gross_amount - fill.commission - fill.stamp_duty)

    def setUp(self) -> None:
        self.engine = ExecutionEngine(fees())

    def test_buy_rounds_to_board_lot_and_charges_minimum_commission(self) -> None:
        book = PositionBook(cash=100_000)
        order = Order(
            order_id="buy-1",
            symbol="000001",
            side=Side.BUY,
            requested_shares=199,
            signal_date=date(2023, 8, 25),
            submit_date=date(2023, 8, 28),
            reason="target_rebalance",
        )
        fill = self.engine.execute(order, Bar.open_bar("000001", date(2023, 8, 28), 10, 1000, 10_000_000), book)

        self.assertEqual(fill.shares, 100)
        self.assertAlmostEqual(fill.price, 10.01)
        self.assertEqual(fill.commission, 5.0)
        self.assertEqual(book.shares("000001"), 100)
        self.assertAlmostEqual(book.cash, 98_994.0)

    def test_buy_slippage_price_rounds_up_to_a_valid_cent(self) -> None:
        order = Order("tick-buy", "000001", Side.BUY, 100,
                      date(2024, 1, 5), date(2024, 1, 8), "test")
        fill = self.engine.execute(
            order,
            Bar.open_bar("000001", date(2024, 1, 8), 12.34, 1000, 1_000_000),
            PositionBook(10_000),
        )

        self.assertEqual(fill.price, 12.36)

    def test_sell_slippage_price_rounds_down_to_a_valid_cent(self) -> None:
        order = Order("tick-sell", "000001", Side.SELL, 100,
                      date(2024, 1, 5), date(2024, 1, 8), "test")
        fill = self.engine.execute(
            order,
            Bar.open_bar("000001", date(2024, 1, 8), 12.34, 1000, 1_000_000),
            PositionBook(0, {"000001": 100}, {"000001": 12.0}),
        )

        self.assertEqual(fill.price, 12.32)

    def test_buy_share_capacity_uses_the_rounded_execution_price(self) -> None:
        order = Order("tick-cash", "000001", Side.BUY, 100,
                      date(2024, 1, 5), date(2024, 1, 8), "test")
        book = PositionBook(1_240.99)
        fill = self.engine.execute(
            order,
            Bar.open_bar("000001", date(2024, 1, 8), 12.34, 1000, 1_000_000),
            book,
        )

        self.assertEqual(fill.status, "rejected")
        self.assertEqual(book.cash, 1_240.99)

    def test_sell_uses_reduced_stamp_duty_after_2023_08_28(self) -> None:
        book = PositionBook(cash=0, holdings={"000001": 100}, average_costs={"000001": 10})
        order = Order(
            order_id="sell-1",
            symbol="000001",
            side=Side.SELL,
            requested_shares=100,
            signal_date=date(2023, 8, 28),
            submit_date=date(2023, 8, 29),
            reason="target_rebalance",
        )
        fill = self.engine.execute(order, Bar.open_bar("000001", date(2023, 8, 29), 10, 1000, 10_000_000), book)

        self.assertEqual(fill.shares, 100)
        self.assertAlmostEqual(fill.stamp_duty, 0.4995)
        self.assertAlmostEqual(book.cash, 993.5005)

    def test_buy_on_one_price_limit_is_rejected_without_cash_change(self) -> None:
        book = PositionBook(cash=100_000)
        order = Order(
            order_id="blocked-1",
            symbol="000001",
            side=Side.BUY,
            requested_shares=100,
            signal_date=date(2024, 1, 5),
            submit_date=date(2024, 1, 8),
            reason="target_rebalance",
        )
        bar = Bar(symbol="000001", trade_date=date(2024, 1, 8), open=11, high=11, low=11, close=11, volume=1000, amount=10_000_000)
        fill = self.engine.execute(order, bar, book, previous_close=10)

        self.assertEqual(fill.status, "rejected")
        self.assertEqual(fill.reason, "one_price_limit_up")
        self.assertEqual(book.cash, 100_000)

    def test_buy_limits_shares_by_minimum_commission(self) -> None:
        book = PositionBook(cash=1_002)
        order = Order(
            order_id="cash-1",
            symbol="000001",
            side=Side.BUY,
            requested_shares=100,
            signal_date=date(2024, 1, 5),
            submit_date=date(2024, 1, 8),
            reason="target_rebalance",
        )

        fill = self.engine.execute(
            order,
            Bar.open_bar("000001", date(2024, 1, 8), 10, 1000, 10_000_000),
            book,
        )

        self.assertEqual(fill.status, "rejected")
        self.assertEqual(fill.reason, "insufficient_cash_or_shares")
        self.assertEqual(book.cash, 1_002)

    def test_invalid_zero_or_nan_price_is_rejected_without_cash_change(self) -> None:
        for price in (0, nan):
            with self.subTest(price=price):
                book = PositionBook(cash=100_000)
                order = Order(
                    order_id=f"price-{price}",
                    symbol="000001",
                    side=Side.BUY,
                    requested_shares=100,
                    signal_date=date(2024, 1, 5),
                    submit_date=date(2024, 1, 8),
                    reason="target_rebalance",
                )

                fill = self.engine.execute(
                    order,
                    Bar.open_bar("000001", date(2024, 1, 8), price, 1000, 10_000_000),
                    book,
                )

                self.assertEqual(fill.status, "rejected")
                self.assertEqual(fill.reason, "invalid_price")
                self.assertEqual(book.cash, 100_000)

    def test_trade_date_must_be_after_signal_and_not_before_submit(self) -> None:
        for signal_date, submit_date, trade_date, reason in (
            (date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 8), "trade_before_signal_date"),
            (date(2024, 1, 8), date(2024, 1, 10), date(2024, 1, 9), "trade_before_submit_date"),
        ):
            with self.subTest(trade_date=trade_date, reason=reason):
                book = PositionBook(cash=100_000)
                order = Order(
                    order_id="timing-1",
                    symbol="000001",
                    side=Side.BUY,
                    requested_shares=100,
                    signal_date=signal_date,
                    submit_date=submit_date,
                    reason="target_rebalance",
                )
                fill = self.engine.execute(
                    order,
                    Bar.open_bar("000001", trade_date, 10, 1000, 10_000_000),
                    book,
                )

                self.assertEqual(fill.status, "rejected")
                self.assertEqual(fill.reason, reason)
                self.assertEqual(book.cash, 100_000)


if __name__ == "__main__":
    unittest.main()
