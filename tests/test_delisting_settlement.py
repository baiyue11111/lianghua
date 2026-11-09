"""Terminal settlement of delisted holdings.

A delisted identity never produces another bar, so an exit order can never fill.
Without a terminal state the position is marked at its last observed close
forever: cash stays out of the book while equity keeps a value that can no
longer be realised.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import unittest

from core.calculation import AllocationLimits
from plugins.data import ResearchBook, Settlement, WriteOff
from tests.fee_fixture import fees, tax_policy, rules


class ResearchWriteOffTests(unittest.TestCase):
    def book(self, *, haircut="0", delistings=None):
        return ResearchBook(
            Decimal("100000"), rules(), AllocationLimits(),
            delistings={"000001": date(2020, 2, 3)} if delistings is None else delistings,
            haircut=haircut,
        )

    def seed(self, book, symbol="000001"):
        book.units[symbol] = Decimal("1000")
        book.basis[symbol] = Decimal("8000")
        book.marks[symbol] = Decimal("3")
        book.raw[symbol] = Decimal("3")
        book.pending_symbols.add(symbol)
        book.target = SimpleNamespace()

    def test_position_is_held_on_the_delisting_day_and_written_off_after(self):
        book = self.book()
        self.seed(book)
        book._write_off(date(2020, 2, 3))
        self.assertEqual(book.units["000001"], Decimal("1000"))
        self.assertEqual(book.writeoffs, [])
        book._write_off(date(2020, 2, 4))
        self.assertNotIn("000001", book.units)

    def test_zero_haircut_removes_units_without_crediting_cash(self):
        book = self.book()
        self.seed(book)
        book._write_off(date(2020, 2, 4))
        self.assertEqual(book.cash, Decimal("100000"))
        self.assertEqual(book.realized, Decimal("-8000"))
        self.assertEqual(book.basis, {})
        self.assertEqual(book.marks, {})
        self.assertEqual(book.raw, {})

    def test_write_off_clears_the_unfillable_pending_symbol(self):
        book = self.book()
        self.seed(book)
        book._write_off(date(2020, 2, 4))
        self.assertEqual(book.pending_symbols, set())
        self.assertIsNone(book.target)

    def test_write_off_is_recorded_with_its_last_mark(self):
        book = self.book()
        self.seed(book)
        book._write_off(date(2020, 2, 4))
        self.assertEqual(book.writeoffs, [WriteOff(
            date(2020, 2, 4), "000001", Decimal("1000"), Decimal("3"),
            Decimal("0"), Decimal("0"), Decimal("8000"),
        )])

    def test_haircut_recovers_a_configured_fraction_of_the_last_mark(self):
        book = self.book(haircut="0.3")
        self.seed(book)
        book._write_off(date(2020, 2, 4))
        self.assertEqual(book.cash, Decimal("100900"))
        self.assertEqual(book.realized, Decimal("-7100"))

    def test_full_haircut_preserves_equity_through_a_cash_posting(self):
        book = self.book(haircut="1")
        self.seed(book)
        before = book.cash + sum(
            (units * book.marks[symbol] for symbol, units in book.units.items()), Decimal(0))
        book._write_off(date(2020, 2, 4))
        after = book.cash + sum(
            (units * book.marks[symbol] for symbol, units in book.units.items()), Decimal(0))
        self.assertEqual(before, after)

    def test_identities_without_a_recorded_delisting_are_untouched(self):
        book = self.book(delistings={})
        self.seed(book)
        book._write_off(date(2030, 1, 1))
        self.assertEqual(book.units["000001"], Decimal("1000"))
        self.assertEqual(book.pending_symbols, {"000001"})

    def test_market_qualified_spelling_resolves_the_same_record(self):
        book = self.book(delistings={"sz.000001": date(2020, 2, 3)})
        self.seed(book)
        book._write_off(date(2020, 2, 4))
        self.assertNotIn("000001", book.units)


class SettlementParameterTests(unittest.TestCase):
    def settlement(self, **overrides):
        return Settlement(Decimal("100000"), fees(), tax_policy(),
                          accounting="research", **overrides)

    def test_describe_records_the_haircut_as_a_declared_parameter(self):
        described = Settlement.describe({"accounting": "research", "delisting_haircut": "0.3"})
        self.assertEqual(described["params"]["delisting_haircut"], "0.3")
        self.assertEqual(described["params"]["accounting"], "research")

    def test_describe_defaults_to_a_full_write_off(self):
        described = Settlement.describe({"accounting": "research"})
        self.assertEqual(described["params"]["delisting_haircut"], "0")

    def test_haircut_outside_the_unit_interval_is_rejected(self):
        for value in ("-0.1", "1.5", "nan", "inf"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.settlement(delisting_haircut=value)

    def test_research_book_receives_dates_and_haircut(self):
        settlement = self.settlement(
            delistings={"000001": "2020-02-03"}, delisting_haircut="0",
        )
        self.assertEqual(settlement._research.delistings, {"000001": date(2020, 2, 3)})
        self.assertEqual(settlement._research.haircut, Decimal("0"))

    def test_dataset_parameters_expose_recorded_dates_only(self):
        scope = {"delistings": {"000001": "2020-02-03"}}
        self.assertEqual(Settlement.dataset_parameters(SimpleNamespace(scope=scope)),
                         {"delistings": {"000001": "2020-02-03"}})
        self.assertEqual(Settlement.dataset_parameters(SimpleNamespace(scope={})),
                         {"delistings": {}})


if __name__ == "__main__":
    unittest.main()
