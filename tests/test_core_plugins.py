from datetime import UTC, datetime
from decimal import Decimal
import unittest

from core.contracts import AccountSnapshot, TargetSet
from core.calculation import Calculation
from plugins.risk.weight_cap import WeightCapRisk


NOW = datetime(2026, 9, 30, tzinfo=UTC)
ACCOUNT = AccountSnapshot(NOW, Decimal("100000"), Decimal("100000"), {})


class AllocationPluginTests(unittest.TestCase):
    def test_equal_weight_preserves_unused_slots_as_cash(self):
        self.assertEqual(Calculation.equal_weights(("A", "B"), 8), {"A": .125, "B": .125})

    def test_empty_selection_stays_empty(self):
        self.assertEqual(Calculation.equal_weights((), 8), {})


class RiskPluginTests(unittest.TestCase):
    def test_weight_cap_clips_without_redistributing(self):
        target = TargetSet("s1", NOW, {"A": 0.6, "B": -0.5, "C": 0.2})

        checked = WeightCapRisk(max_absolute_weight=0.4).apply(target, ACCOUNT)

        self.assertEqual(checked.weights, {"A": 0.4, "B": -0.4, "C": 0.2})

    def test_invalid_plugin_parameters_fail_during_composition(self):
        with self.assertRaises(ValueError):
            Calculation.equal_weights(("A",), 8, gross=-0.1)
        with self.assertRaises(ValueError):
            WeightCapRisk(max_absolute_weight=0.0)


if __name__ == "__main__":
    unittest.main()
