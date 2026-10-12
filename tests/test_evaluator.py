from datetime import date
import unittest

from core.calculation import Calculation


class EvaluatorTests(unittest.TestCase):
    def test_equity_metrics_reconcile_drawdown_and_observation_count(self) -> None:
        metrics = Calculation.evaluate(
            [(date(2024, 1, 2), 100_000.0), (date(2024, 1, 3), 110_000.0), (date(2024, 1, 4), 99_000.0)]
        )
        self.assertEqual(metrics["observations"], 3)
        self.assertAlmostEqual(metrics["max_drawdown"], -0.1)
        self.assertIn("sharpe", metrics)


if __name__ == "__main__":
    unittest.main()
