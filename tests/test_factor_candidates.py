from __future__ import annotations

import json
import math
import unittest

from factor_candidates import (
    FACTOR_NAMES,
    FactorDefinition,
    factor_spec_hash,
    factor_spec_json,
    score_factor_candidates,
)


def _features(symbols: tuple[str, ...] = ("000001", "000002", "000003")) -> dict[str, dict[str, float]]:
    return {
        symbol: {
            "momentum_20": float(index + 1),
            "momentum_60": float(index + 2),
            "momentum_120": float(index + 3),
            "momentum_252": float(index + 4),
            "reversal_5": float(index + 1),
            "reversal_20": float(index + 2),
            "reversal_60": float(index + 3),
            "low_volatility_20": float(index + 1),
            "low_volatility_60": float(index + 2),
            "low_amount_20": float(index + 3),
        }
        for index, symbol in enumerate(symbols)
    }


class FactorCandidateTests(unittest.TestCase):
    def test_defines_exactly_the_frozen_twelve_candidates(self) -> None:
        self.assertEqual(
            FACTOR_NAMES,
            (
                "momentum_20",
                "momentum_60",
                "momentum_120",
                "momentum_252",
                "reversal_5",
                "reversal_20",
                "reversal_60",
                "low_volatility_20",
                "low_volatility_60",
                "low_amount_20",
                "momentum120_lowvol60",
                "reversal20_lowamount20",
            ),
        )

    def test_direction_is_explicit_and_scores_are_cross_sectional_percentiles(self) -> None:
        features = _features()
        result = score_factor_candidates(features)

        momentum = result.factor("momentum_20")
        self.assertEqual(momentum.score("000001"), 0.0)
        self.assertEqual(momentum.score("000002"), 0.5)
        self.assertEqual(momentum.score("000003"), 1.0)

        reversal = result.factor("reversal_20")
        self.assertEqual(reversal.score("000001"), 1.0)
        self.assertEqual(reversal.score("000002"), 0.5)
        self.assertEqual(reversal.score("000003"), 0.0)

        low_amount = result.factor("low_amount_20")
        self.assertEqual(low_amount.score("000001"), 1.0)
        self.assertEqual(low_amount.score("000003"), 0.0)

    def test_combinations_rank_only_on_the_common_valid_set(self) -> None:
        features = _features(("000001", "000002", "000003", "000004"))
        features["000004"]["momentum_120"] = math.nan
        features["000001"]["low_volatility_60"] = math.inf
        features["000002"]["low_volatility_60"] = 9.0
        features["000003"]["low_volatility_60"] = 1.0
        result = score_factor_candidates(features)

        combined = result.factor("momentum120_lowvol60")
        self.assertTrue(combined.valid("000002"))
        self.assertTrue(combined.valid("000003"))
        self.assertFalse(combined.valid("000001"))
        self.assertFalse(combined.valid("000004"))
        # The two common rows are the min/max of each component, so their
        # equal-weight score is exactly 0.0 and 1.0.
        self.assertEqual(combined.score("000002"), 0.0)
        self.assertEqual(combined.score("000003"), 1.0)

    def test_missing_nonfinite_values_propagate_without_zero_filling(self) -> None:
        features = _features()
        features["000001"].pop("momentum_20")
        features["000002"]["momentum_20"] = math.nan
        features["000003"]["momentum_20"] = math.inf
        result = score_factor_candidates(features)
        factor = result.factor("momentum_20")

        for symbol in features:
            self.assertFalse(factor.valid(symbol))
            self.assertIsNone(factor.score(symbol))
        self.assertEqual(factor.reason("000001"), "missing:momentum_20")
        self.assertEqual(factor.reason("000002"), "non_finite:momentum_20")
        self.assertEqual(factor.reason("000003"), "non_finite:momentum_20")

    def test_positive_scale_and_input_order_do_not_change_scores(self) -> None:
        original = _features()
        scaled = {
            symbol: {name: value * 1000.0 for name, value in row.items()}
            for symbol, row in reversed(tuple(original.items()))
        }
        first = score_factor_candidates(original)
        second = score_factor_candidates(scaled)

        for factor_name in FACTOR_NAMES:
            left = first.factor(factor_name)
            right = second.factor(factor_name)
            self.assertEqual(left.rows, right.rows)

    def test_ties_use_average_percentile_and_symbol_order_is_deterministic(self) -> None:
        features = _features(("000002", "000001", "000003"))
        features["000001"]["momentum_20"] = 2.0
        features["000002"]["momentum_20"] = 2.0
        result = score_factor_candidates(features)
        factor = result.factor("momentum_20")

        self.assertEqual(factor.score("000001"), 0.25)
        self.assertEqual(factor.score("000002"), 0.25)
        self.assertEqual(factor.ordered_symbols, ("000003", "000001", "000002"))

    def test_result_and_spec_are_serializable_and_spec_hash_is_stable(self) -> None:
        result = score_factor_candidates(_features())
        self.assertIsInstance(factor_spec_json(), str)
        self.assertEqual(len(factor_spec_hash()), 64)
        spec = json.loads(factor_spec_json())
        self.assertEqual(spec["rules"]["percentile"], "average_one_based_rank_normalized")
        self.assertEqual(spec["rules"]["tie_policy"], "exact_value_average_rank")
        self.assertEqual(spec["rules"]["single_name_percentile"], 0.5)
        self.assertEqual(spec["rules"]["missing_policy"], "invalid_no_score")
        encoded = result.to_dict()
        self.assertEqual(encoded["spec_hash"], factor_spec_hash())
        self.assertEqual(encoded["spec_json"], factor_spec_json())
        self.assertEqual(tuple(item["name"] for item in encoded["spec"]), FACTOR_NAMES)
        self.assertEqual(tuple(encoded["factors"]), FACTOR_NAMES)
        self.assertEqual(encoded["status"], "UNTESTED_CANDIDATE")

    def test_input_mapping_is_not_mutated(self) -> None:
        features = _features()
        before = {symbol: dict(row) for symbol, row in features.items()}
        score_factor_candidates(features)
        self.assertEqual(features, before)

    def test_factor_definition_rejects_each_kind_of_length_mismatch(self) -> None:
        invalid_definitions = (
            (("a", "b"), ("higher",), (1.0,)),
            (("a",), ("higher", "lower"), (0.5, 0.5)),
            (("a",), ("higher",), (0.5, 0.5)),
        )
        for inputs, directions, weights in invalid_definitions:
            with self.subTest(inputs=inputs, directions=directions, weights=weights):
                with self.assertRaises(ValueError):
                    FactorDefinition("invalid", inputs, directions, weights)


if __name__ == "__main__":
    unittest.main()
