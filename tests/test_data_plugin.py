import unittest

import numpy as np

from plugins.data import Scaler


class ScalerTests(unittest.TestCase):
    def test_standard_matches_independent_training_reference(self):
        train = np.array([[1.0, 10.0], [3.0, 14.0], [5.0, 18.0]])
        scaler = Scaler("standard").fit(train)

        expected_center = np.array([3.0, 14.0])
        expected_scale = np.array([np.sqrt(8 / 3), np.sqrt(32 / 3)])
        np.testing.assert_allclose(scaler.center_, expected_center)
        np.testing.assert_allclose(scaler.scale_, expected_scale)
        np.testing.assert_allclose(
            scaler.transform(np.array([[7.0, 22.0]])),
            (np.array([[7.0, 22.0]]) - expected_center) / expected_scale,
        )

    def test_validation_data_does_not_change_training_statistics(self):
        train = np.array([[0.0], [2.0], [4.0]])
        validation = np.array([[100.0]])
        scaler = Scaler("standard").fit(train)

        expected = (validation - 2.0) / np.sqrt(8 / 3)
        np.testing.assert_allclose(scaler.transform(validation), expected)
        np.testing.assert_allclose(scaler.center_, [2.0])
        np.testing.assert_allclose(scaler.scale_, [np.sqrt(8 / 3)])

    def test_minmax_uses_configured_range_without_clipping(self):
        scaler = Scaler("minmax", feature_range=(-2.0, 3.0)).fit([[0.0], [10.0]])

        np.testing.assert_allclose(scaler.transform([[-5.0], [5.0], [15.0]]), [[-4.5], [0.5], [5.5]])

    def test_robust_uses_configured_quantiles(self):
        scaler = Scaler("robust", quantile_range=(20.0, 80.0)).fit([[1.0], [2.0], [3.0], [4.0], [100.0]])

        low = np.quantile([1.0, 2.0, 3.0, 4.0, 100.0], 0.2)
        high = np.quantile([1.0, 2.0, 3.0, 4.0, 100.0], 0.8)
        expected = (np.array([[2.0], [3.0], [4.0]]) - 3.0) / (high - low)
        np.testing.assert_allclose(scaler.transform([[2.0], [3.0], [4.0]]), expected)

    def test_constant_columns_have_scale_one_and_explicit_outputs(self):
        train = [[4.0, 8.0], [4.0, 8.0]]
        validation = [[6.0, 10.0]]

        for method in ("standard", "robust"):
            scaler = Scaler(method).fit(train)
            np.testing.assert_array_equal(scaler.scale_, [1.0, 1.0])
            np.testing.assert_allclose(scaler.transform([[4.0, 8.0]]), [[0.0, 0.0]])
            np.testing.assert_allclose(scaler.transform(validation), [[2.0, 2.0]])

        scaler = Scaler("minmax", feature_range=(2.0, 6.0)).fit(train)
        np.testing.assert_array_equal(scaler.scale_, [1.0, 1.0])
        np.testing.assert_allclose(scaler.transform([[4.0, 8.0]]), [[2.0, 2.0]])
        np.testing.assert_allclose(scaler.transform(validation), [[10.0, 10.0]])

    def test_refit_replaces_all_training_statistics(self):
        scaler = Scaler("standard").fit([[0.0], [2.0]])
        scaler.fit([[10.0], [20.0]])

        np.testing.assert_allclose(scaler.center_, [15.0])
        np.testing.assert_allclose(scaler.scale_, [5.0])
        np.testing.assert_allclose(scaler.transform([[20.0]]), [[1.0]])

    def test_fit_rejects_overflow_without_saving_invalid_statistics(self):
        bad_inputs = {
            "standard": [[1e308], [1e308]],
            "minmax": [[-1e308], [1e308]],
            "robust": [[-1e308], [1e308]],
        }
        for method, values in bad_inputs.items():
            with self.subTest(method=method), self.assertRaises(ValueError):
                Scaler(method).fit(values)

        scaler = Scaler("standard").fit([[0.0], [2.0]])
        old_center = scaler.center_.copy()
        old_scale = scaler.scale_.copy()
        with self.assertRaises(ValueError):
            scaler.fit([[1e308], [1e308]])
        np.testing.assert_array_equal(scaler.center_, old_center)
        np.testing.assert_array_equal(scaler.scale_, old_scale)
        np.testing.assert_allclose(scaler.transform([[2.0]]), [[1.0]])

    def test_failed_refit_preserves_statistics_and_feature_count_for_each_method(self):
        for method in ("standard", "minmax", "robust"):
            with self.subTest(method=method):
                scaler = Scaler(method).fit([[1.0, 10.0], [3.0, 14.0]])
                old_center = scaler.center_.copy()
                old_scale = scaler.scale_.copy()
                old_count = scaler.n_features_in_
                with self.assertRaises(ValueError):
                    scaler.fit([[-1e308, -1e308], [1e308, 1e308]])
                np.testing.assert_array_equal(scaler.center_, old_center)
                np.testing.assert_array_equal(scaler.scale_, old_scale)
                self.assertEqual(scaler.n_features_in_, old_count)

    def test_transform_rejects_overflow_and_nonfinite_output(self):
        scaler = Scaler("standard").fit([[-1e308]])
        with self.assertRaises(ValueError):
            scaler.transform([[1e308]])
        with self.assertRaises(ValueError):
            Scaler("minmax", feature_range=(-1e308, 1e308))

    def test_bool_is_rejected_before_numpy_numeric_coercion(self):
        with self.assertRaises(ValueError):
            Scaler().fit([[True], [2.0]])

    def test_fit_and_transform_reject_invalid_shapes_values_and_parameters(self):
        with self.assertRaisesRegex(ValueError, "not fitted"):
            Scaler().transform([[1.0]])
        for values in ([], [[1.0, 2.0], [3.0]], [[1.0], [np.nan]], [[1.0], [np.inf]], [["1.0"]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Scaler().fit(values)
        scaler = Scaler().fit([[1.0, 2.0]])
        with self.assertRaises(ValueError):
            scaler.transform([[1.0]])
        with self.assertRaises(ValueError):
            scaler.transform([[1.0, np.nan]])
        with self.assertRaises(ValueError):
            Scaler("other")
        with self.assertRaises(ValueError):
            Scaler("minmax", feature_range=(1.0, 1.0))
        with self.assertRaises(ValueError):
            Scaler("robust", quantile_range=(75.0, 25.0))


if __name__ == "__main__":
    unittest.main()
