"""Independent acceptance of a single public trade-calculation surface."""
import unittest
from copy import deepcopy
from datetime import date
from decimal import Decimal as D

from protocol import Bar, Order, Side
from tests.fee_fixture import fees, tax_policy


def config():
    return {
        "price": {"model": "ratio", "params": {"rate": "0.001", "tick": "0.01"}},
        "quantity": {"model": "capacity", "params": {}},
        "fees": {"model": "dated", "params": {"rounding": "none"}},
        "tax": {"model": "fifo", "params": {}},
        "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}},
    }


class TradeRulesAcceptanceTests(unittest.TestCase):
    def rules(self, value=None):
        from core.calculation import TradeRules
        return TradeRules(execution=deepcopy(value or config()), fee_policy=fees(), tax_policy=tax_policy())

    def order(self, side=Side.BUY, quantity=1000):
        return Order("acceptance", "ANY", side, quantity, date(2024, 1, 2), date(2024, 1, 3), "test")

    def bar(self):
        return Bar("ANY", date(2024, 1, 3), 10, 11, 9, 10, 100000, 1000000)

    def test_one_class_provides_the_five_trade_actions(self):
        from core import calculation
        rules = getattr(calculation, "TradeRules", None)
        self.assertIsNotNone(rules, "all trade algorithms must live in TradeRules")
        for action in ("price", "quantity", "fees", "tax", "check"):
            with self.subTest(action=action):
                self.assertTrue(callable(getattr(rules, action, None)))

    def test_price_models_have_independent_hand_calculated_answers(self):
        cases = (
            ("ratio", {"rate": "0.001", "tick": "0.01"}, "10.01", "9.99"),
            ("tick", {"ticks": 2, "tick": "0.01"}, "10.02", "9.98"),
            ("volume_share", {"impact": "0.1", "tick": "0.01", "capacity_basis": "prior20"}, "10.04", "9.96"),
        )
        for model, params, buy, sell in cases:
            with self.subTest(model=model):
                value = config()
                value["price"] = {"model": model, "params": params}
                rules = self.rules(value)
                self.assertEqual(rules.price(self.order(), self.bar(), 400, D("10"), volume20=D("2000")), D(buy))
                self.assertEqual(rules.price(self.order(Side.SELL), self.bar(), 400, D("10"), volume20=D("2000")), D(sell))

    def test_capacity_charges_minimum_commission_once_not_per_lot(self):
        rules = self.rules()
        result = rules.quantity(self.order(), self.bar(), price=D("10"), cash=D("10005"),
                                positions={}, sellable={}, reserve=D(0), volume20=None)
        self.assertEqual(result, 1000)

    def test_quantity_minimum_and_increment_are_separate_rules(self):
        value = config()
        value["check"]["params"].update(min_qty=200, step=1)
        result = self.rules(value).quantity(self.order(quantity=217), self.bar(), price=D("10"),
                                           cash=D("10000"), positions={}, sellable={}, reserve=D(0), volume20=None)
        self.assertEqual(result, 217)

    def test_quantity_increment_is_counted_from_minimum_not_zero(self):
        value = config()
        value["check"]["params"].update(min_qty=200, step=3)
        result = self.rules(value).quantity(self.order(quantity=217), self.bar(), price=D("10"),
                                           cash=D("10000"), positions={}, sellable={}, reserve=D(0), volume20=None)
        self.assertEqual(result, 215)

    def test_check_accepts_the_same_minimum_based_grid_as_quantity(self):
        value = config()
        value["check"]["params"].update(min_qty=200, step=3)
        rules = self.rules(value)
        reason = rules.check(self.order(quantity=217), self.bar(), D("10"),
                             price=D("10"), quantity=215, positions={}, sellable={})
        self.assertIsNone(reason)

    def test_sell_can_dispose_of_the_complete_odd_balance(self):
        rules = self.rules()
        for quantity in (50, 150):
            with self.subTest(quantity=quantity):
                result = rules.quantity(self.order(Side.SELL, quantity), self.bar(), price=D("10"),
                                        cash=D(0), positions={"ANY": quantity}, sellable={"ANY": quantity},
                                        reserve=D(0), volume20=None)
                self.assertEqual(result, quantity)

    def test_sell_disposes_of_an_odd_remainder_once_without_splitting_it(self):
        rules = self.rules()
        for requested, expected in ((50, 50), (30, 0), (120, 100), (150, 150)):
            with self.subTest(requested=requested):
                result = rules.quantity(self.order(Side.SELL, requested), self.bar(), price=D("10"),
                                        cash=D(0), positions={"ANY": 150}, sellable={"ANY": 150},
                                        reserve=D(0), volume20=None)
                self.assertEqual(result, expected)

    def test_participation_also_respects_the_available_cash(self):
        value = config()
        value["quantity"] = {"model": "participation", "params": {"participation": "0.2", "capacity_basis": "prior20"}}
        result = self.rules(value).quantity(self.order(), self.bar(), price=D("10"), cash=D("1005"),
                                           positions={}, sellable={}, reserve=D(0), volume20=D("10000"))
        self.assertEqual(result, 100)

    def test_volume_price_and_cash_are_solved_on_the_legal_quantity_grid(self):
        value = config()
        value["price"] = {"model": "volume_share", "params": {"impact": "0.1", "tick": "0.01", "capacity_basis": "prior20"}}
        value["check"]["params"].update(min_qty=200, step=3)
        result = self.rules(value).quantity(
            self.order(quantity=217), self.bar(), price=D("10"), previous_close=D("10"),
            cash=D("2170"), positions={}, sellable={}, reserve=D(0), volume20=D("1000000"),
        )
        self.assertEqual(result, 215)

    def test_requested_quantity_does_not_overprice_a_small_affordable_fill(self):
        value = config()
        value["price"] = {"model": "volume_share", "params": {"impact": "0.1", "tick": "0.01", "capacity_basis": "prior20"}}
        result = self.rules(value).quantity(
            self.order(quantity=100000), self.bar(), price=D("10"), previous_close=D("10"),
            cash=D("10015"), positions={}, sellable={}, reserve=D(0), volume20=D("10000"),
        )
        self.assertEqual(result, 1000)

    def test_unsupported_model_is_rejected_when_rules_are_created(self):
        value = config()
        value["price"]["model"] = "unknown"
        with self.assertRaises(ValueError):
            self.rules(value)

    def test_unknown_model_parameter_is_rejected_before_trading(self):
        value = config()
        value["price"]["params"]["unknown"] = 1
        with self.assertRaises(ValueError):
            self.rules(value)

    def test_fee_rounding_is_selected_by_execution_not_hidden_in_policy(self):
        value = config()
        value["fees"]["params"]["rounding"] = "half_up"
        result = self.rules(value).fees(D("123.456"), Side.SELL, date(2024, 1, 3))
        self.assertEqual(result.commission, D("5.00"))
        self.assertEqual(result.stamp_duty, D("0.06"))

    def test_volume_model_requires_a_past_reference(self):
        value = config()
        value["price"] = {"model": "volume_share", "params": {"impact": "0.1", "tick": "0.01", "capacity_basis": "prior20"}}
        with self.assertRaises(ValueError):
            self.rules(value).price(self.order(), self.bar(), 100, D("10"), volume20=None)

    def test_execution_evidence_changes_when_the_selected_model_changes(self):
        from core.kernel import Kernel
        from protocol import DataQuery
        from tests.test_kernel_search import SearchData, SearchStrategy, SearchSettlement, request
        kernel = Kernel(
            SearchData(), lambda _name, params: SearchStrategy(params),
            lambda req, requirements: DataQuery(
                req.dataset_id, req.start.date(), req.end.date(), requirements.fields,
                requirements.history_bars, requirements.price_basis,
                req.as_of or req.end.date(), frequency=requirements.frequency,
                market=req.market,
            ), SearchSettlement,
        )
        ratio = config()
        tick = config()
        tick["price"] = {"model": "tick", "params": {"ticks": 2, "tick": "0.01"}}
        left = kernel.prepare(request(settlement_parameters={"execution": ratio}))
        right = kernel.prepare(request(settlement_parameters={"execution": tick}))
        self.assertIsNotNone(left.dataset.provenance.get("execution_hash"))
        self.assertNotEqual(left.dataset.provenance.get("execution_hash"), right.dataset.provenance.get("execution_hash"))

    def test_two_orders_share_daily_capacity_and_replay_does_not_consume_it_again(self):
        from core.calculation import AccountExecutionSession, AccountLedger, AllocationLimits, OpeningRiskSnapshot
        value = config()
        value["quantity"] = {"model": "participation", "params": {"participation": "0.2", "capacity_basis": "prior20"}}
        account = AccountLedger(D("10000"))
        session = AccountExecutionSession(account, self.rules(value))
        for identity in ("first", "second"):
            session.submit(Order(identity, "ANY", Side.BUY, 150, date(2024, 1, 2), date(2024, 1, 3), "test"))
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {"ANY": D(10)}, {"ANY": D("1000000")}, D(0), D(0),
                                   AllocationLimits(stock_weight=D(1), adv_fraction=D(1)))
        arguments = {"risk": risk, "volume20": {"ANY": D(1000)}}
        fills = session.execute_open(date(2024, 1, 3), {"ANY": self.bar()}, {"ANY": 10}, **arguments)
        self.assertEqual([fill.shares for fill in fills], [100, 100])
        cash, postings = account.cash, account.cash_events
        repeated = session.execute_open(date(2024, 1, 3), {"ANY": self.bar()}, {"ANY": 10}, **arguments)
        self.assertEqual(repeated, fills)
        self.assertEqual(account.cash, cash)
        self.assertEqual(account.cash_events, postings)

    def test_replay_rejects_a_changed_explicit_cash_reserve(self):
        from core.calculation import AccountExecutionSession, AccountLedger, AllocationLimits, OpeningRiskSnapshot
        account = AccountLedger(D("10000"))
        session = AccountExecutionSession(account, self.rules())
        session.submit(self.order(quantity=100))
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {"ANY": D(10)}, {"ANY": D("1000000")}, D(0), D(0),
                                   AllocationLimits(stock_weight=D(1), adv_fraction=D(1)))
        session.execute_open(date(2024, 1, 3), {"ANY": self.bar()}, {"ANY": 10}, risk=risk, cash_reserve=D(0))
        cash, postings = account.cash, account.cash_events
        with self.assertRaises(ValueError):
            session.execute_open(date(2024, 1, 3), {"ANY": self.bar()}, {"ANY": 10}, risk=risk, cash_reserve=D(9000))
        self.assertEqual(account.cash, cash)
        self.assertEqual(account.cash_events, postings)

    def test_preview_failure_does_not_post_cash_or_consume_the_order(self):
        from core.calculation import AccountExecutionSession, AccountLedger, AllocationLimits, OpeningRiskSnapshot
        account = AccountLedger(D("10000"))
        rules = self.rules()
        rules.tax(date(2024, 1, 3), phase="begin")
        session = AccountExecutionSession(account, rules)
        session.submit(self.order(quantity=100))
        risk = OpeningRiskSnapshot(date(2024, 1, 2), {"ANY": D(10)}, {"ANY": D("1000000")}, D(0), D(0),
                                   AllocationLimits(stock_weight=D(1), adv_fraction=D(1)))
        def fail(_fill):
            rules._tax_ledger.pending["partial"] = "must be rolled back"
            raise ValueError("preview failed")
        with self.assertRaisesRegex(ValueError, "preview failed"):
            session.execute_open(date(2024, 1, 3), {"ANY": self.bar()}, {"ANY": 10}, risk=risk, on_fill=fail)
        self.assertEqual(account.cash, D("10000"))
        self.assertEqual(account.lots, ())
        self.assertEqual(account.cash_events, ())
        self.assertEqual(session.fills, ())
        self.assertEqual(len(session.pending), 1)
        self.assertEqual(rules._tax_ledger.pending, {})


if __name__ == "__main__":
    unittest.main()
