from datetime import date
from dataclasses import replace
from decimal import Decimal
import unittest
import core.calculation as actions

from core.calculation import (
    InvalidCorporateAction,
    adjustment_factor_asof,
    normalize_dividend_records,
)


class CorporateActionTests(unittest.TestCase):
    def test_entitlement_uses_registration_snapshot_and_separate_due_dates(self):
        event = normalize_dividend_records([{
            "code": "sz.000010", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-03",
            "stock_market_date": "2020-07-06", "cash_per_share": "0.3",
            "stock_dividend_per_share": "0.1", "reserve_to_stock_per_share": "0.2",
        }])[0]
        entitlement = actions.calculate_entitlement(event, date(2020, 7, 1), 150)
        self.assertEqual(entitlement.gross_cash, Decimal('45'))
        self.assertEqual(entitlement.bonus_shares, Decimal('15'))
        self.assertEqual(entitlement.reserve_shares, Decimal('30'))
        self.assertEqual(entitlement.cash_due, date(2020, 7, 3))
        self.assertEqual(entitlement.shares_due, date(2020, 7, 6))
        self.assertFalse(entitlement.tax_verified)
        with self.assertRaises(InvalidCorporateAction):
            actions.calculate_entitlement(event, date(2020, 7, 2), 150)
        with self.assertRaises(InvalidCorporateAction):
            actions.calculate_entitlement(event, date(2020, 7, 1), -1)
        fractional = actions.calculate_entitlement(event, date(2020, 7, 1), 101)
        self.assertEqual(fractional.bonus_shares, Decimal('10.1'))
        self.assertTrue(fractional.requires_fractional_allocation)
        self.assertFalse(entitlement.requires_fractional_allocation)

    def test_alias_dividends_merge_once_with_both_sources(self):
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0,
            "code": "sz.000010", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-02",
            "cash_per_share": "0.3",
        }])[0]
        observations = (("old-source", "annual-2019", event),
                        ("new-source", "annual-2019", replace(event, code="sz.001010")))
        identities = {"sz.000010": "security-one", "sz.001010": "security-one"}
        groups = actions.reconcile_corporate_actions(observations, identities)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].security_id, "security-one")
        self.assertEqual(groups[0].distribution_id, "annual-2019")
        self.assertEqual(groups[0].event.cash_per_share, 0.3)
        self.assertEqual({source for source, _ in groups[0].observations}, {"old-source", "new-source"})
        self.assertEqual(groups, actions.reconcile_corporate_actions(tuple(reversed(observations)), identities))
        with self.assertRaises(InvalidCorporateAction):
            actions.reconcile_corporate_actions((observations[0], ("conflict", "annual-2019", replace(event, cash_per_share=0.4))), identities)
        with self.assertRaises(KeyError):
            actions.reconcile_corporate_actions(observations, {})

    def test_distinct_plans_same_day_are_not_duplicate_or_conflicting(self):
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0,
            "code": "sz.000010", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-02",
            "cash_per_share": "0.021",
        }])[0]
        observations = (("source-row-0", "annual-2019", event),
                        ("source-row-1", "interim-2020", replace(event, cash_per_share=0.041)))
        groups = actions.reconcile_corporate_actions(observations, {event.code: "security-one"})
        self.assertEqual(len(groups), 2)
        self.assertEqual({g.distribution_id for g in groups}, {"annual-2019", "interim-2020"})
        claims = [actions.calculate_entitlement(g.event, date(2020, 7, 1), 100).gross_cash for g in groups]
        self.assertEqual(sum(claims), Decimal('6.200'))
        self.assertEqual(groups, actions.reconcile_corporate_actions(observations[::-1], {event.code: "security-one"}))
        with self.assertRaises(InvalidCorporateAction):
            actions.reconcile_corporate_actions((("source", "", event),), {event.code: "security-one"})

    def test_invalid_settlement_chronology_blocks_execution(self):
        base = {'reserve_to_stock_per_share': 0,
            "code": "sh.600000", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-02",
            "stock_market_date": "2020-07-02", "cash_per_share": "1",
            "stock_dividend_per_share": "0.1",
        }
        for field, value, issue in (
            ("registration_date", "2020-07-03", "registration_not_before_operation"),
            ("registration_date", "2020-07-02", "registration_not_before_operation"),
            ("payment_date", "2020-07-01", "payment_before_operation"),
            ("stock_market_date", "2020-07-01", "stock_market_before_operation"),
        ):
            with self.subTest(field=field, value=value):
                event = normalize_dividend_records([{**base, field: value}])[0]
                self.assertFalse(event.executable)
                self.assertIn(issue, event.issues)
                self.assertEqual(event.cash_per_share, 1)
        self.assertTrue(normalize_dividend_records([base])[0].executable)

    def test_normalization_never_implies_tax_verification(self):
        for tax_text in (None, "", "0.9", "0.9或1"):
            with self.subTest(tax_text=tax_text):
                event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0,
                    "code": "sh.600000", "registration_date": "2020-07-01",
                    "operation_date": "2020-07-02", "payment_date": "2020-07-02",
                    "cash_per_share": "1", "cash_after_tax_text": tax_text,
                }])[0]
                self.assertIn("tax_unverified", event.issues)
                self.assertEqual(event.cash_per_share, 1.0)

    def test_cash_only_event_does_not_change_share_ratio(self):
        event = normalize_dividend_records([{
            "code": "sh.600000",
            "registration_date": "2014-06-23",
            "operation_date": "2014-06-24",
            "payment_date": "2014-06-24",
            "stock_market_date": "",
            "cash_per_share": "0.66",
            "stock_dividend_per_share": "0",
            "reserve_to_stock_per_share": 0,
        }])[0]
        self.assertEqual(event.cash_per_share, 0.66)
        self.assertEqual(event.share_ratio, 1.0)
        self.assertEqual(event.stock_dividend_per_share, 0.0)
        self.assertTrue(event.executable)
        self.assertNotIn("stock_market_date", event.issues)

    def test_stock_distribution_and_cash_are_separate(self):
        event = normalize_dividend_records([{
            "code": "sh.600000", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-03",
            "stock_market_date": "2020-07-06", "cash_per_share": "0.41",
            "stock_dividend_per_share": "0.2", "reserve_to_stock_per_share": "0.3",
        }])[0]
        self.assertEqual(event.cash_per_share, 0.41)
        self.assertEqual(event.share_ratio, 1.5)
        self.assertEqual(event.stock_dividend_per_share, 0.2)
        self.assertEqual(event.reserve_to_stock_per_share, 0.3)

    def test_real_2025_gross_cash_is_kept_when_net_tax_text_is_ambiguous(self):
        event = normalize_dividend_records([{
            "code": "sh.600000", "registration_date": "2025-07-15",
            "operation_date": "2025-07-16", "payment_date": "2025-07-16",
            "stock_market_date": "", "cash_per_share": "0.41",
            "cash_after_tax_text": "0.369或0.41", "stock_dividend_per_share": "0.000000",
            "reserve_to_stock_per_share": 0,
        }])[0]
        self.assertEqual(event.cash_per_share, 0.41)
        self.assertEqual(event.share_ratio, 1.0)
        self.assertEqual(event.cash_after_tax_text, "0.369或0.41")
        self.assertIn("tax_unverified", event.issues)
        self.assertTrue(event.executable)

    def test_stock_only_event_does_not_require_payment_date(self):
        event = normalize_dividend_records([{
            "code": "sh.600000", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "stock_market_date": "2020-07-06",
            "cash_per_share": 0, "stock_dividend_per_share": "0.2",
            "reserve_to_stock_per_share": "0.3",
        }])[0]
        self.assertTrue(event.executable)
        self.assertIsNone(event.payment_date)

    def test_all_economic_terms_missing_are_rejected(self):
        with self.assertRaises(InvalidCorporateAction):
            normalize_dividend_records([{
                "code": "sh.600000", "operation_date": "2020-07-02",
            }])

    def test_missing_or_null_economic_values_are_rejected(self):
        base = {
            "code": "sh.600000", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-03",
            "cash_per_share": 1, "stock_dividend_per_share": 0,
            "reserve_to_stock_per_share": 0,
        }
        for field in ("cash_per_share", "stock_dividend_per_share", "reserve_to_stock_per_share"):
            for missing in (True, False):
                with self.subTest(field=field, missing=missing):
                    row = dict(base)
                    if missing:
                        del row[field]
                    else:
                        row[field] = None
                    with self.assertRaises(InvalidCorporateAction):
                        normalize_dividend_records([row])

    def test_supplier_field_names_are_not_accepted(self):
        with self.assertRaises(InvalidCorporateAction):
            normalize_dividend_records([{
                "code": "sh.600000", "dividOperateDate": "2020-07-02",
                "dividCashPsBeforeTax": "0.66",
            }])
        with self.assertRaises(InvalidCorporateAction):
            adjustment_factor_asof("sh.600000", [{
                "code": "sh.600000", "dividOperateDate": "2020-01-01",
                "foreAdjustFactor": "2", "backAdjustFactor": "2",
            }], date(2020, 1, 2), date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.5)

    def test_factor_is_asof_and_not_effective_before_operation_date(self):
        records = [{"code": "sh.600000", "date": "2020-07-02",
                    "forward": "2", "backward": "3"}]
        before = adjustment_factor_asof(
            "sh.600000", records, date(2020, 7, 1), date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.75)
        after = adjustment_factor_asof(
            "sh.600000", records, date(2020, 7, 2), date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.75)
        self.assertEqual((before.fore_factor, before.back_factor), (0.5, 0.75))
        self.assertEqual((after.fore_factor, after.back_factor), (2.0, 3.0))

    def test_missing_coverage_cannot_default_factor_to_one(self):
        with self.assertRaises(InvalidCorporateAction):
            adjustment_factor_asof("sh.600000", [], date(2020, 1, 1), None, date(2020, 12, 31), None, None)

    def test_empty_factor_records_keep_explicit_identity_and_anchor_source(self):
        signal = adjustment_factor_asof("sh.600000", [], date(2020, 1, 1),
                                        date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.75)
        self.assertEqual(signal.code, "sh.600000")
        self.assertEqual(signal.anchor_source, "explicit_input")
        self.assertIsNone(signal.source_operation_date)

    def test_factor_asof_rejects_mixed_or_empty_record_codes(self):
        with self.assertRaises(InvalidCorporateAction):
            adjustment_factor_asof("sh.600000", [{"code": "", "date": "2020-01-01",
                "forward": "2", "backward": "2"}], date(2020, 1, 2),
                date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.5)
        with self.assertRaises(InvalidCorporateAction):
            adjustment_factor_asof("sh.600000", [
                {"code": "sh.600000", "date": "2020-01-01", "forward": "2", "backward": "2"},
                {"code": "sh.600001", "date": "2020-02-01", "forward": "3", "backward": "3"}],
                date(2020, 2, 2), date(1990, 1, 1), date(2020, 12, 31), 0.5, 0.5)

    def test_ambiguous_after_tax_value_is_rejected(self):
        event = normalize_dividend_records([{'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0,
            "code": "sh.600000", "registration_date": "2020-07-01",
            "operation_date": "2020-07-02", "payment_date": "2020-07-03",
            "cash_per_share": "0.66", "cash_after_tax_text": "0.594或0.627",
        }])[0]
        self.assertEqual(event.cash_per_share, 0.66)
        self.assertIn("tax_unverified", event.issues)

    def test_duplicate_conflict_is_rejected(self):
        row = {'reserve_to_stock_per_share': 0, 'stock_dividend_per_share': 0, "code": "sh.600000", "operation_date": "2020-07-02",
               "cash_per_share": "0.66"}
        with self.assertRaises(InvalidCorporateAction):
            normalize_dividend_records([row, {**row, "cash_per_share": "0.67"}])
