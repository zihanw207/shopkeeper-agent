import unittest
from decimal import Decimal

from app.evaluation.results import results_equal


class ResultComparisonTests(unittest.TestCase):
    def test_ignores_aliases_and_unordered_row_order(self):
        self.assertTrue(
            results_equal(
                [{"地区": "华北", "金额": "2.001"}, {"地区": "华东", "金额": 3}],
                [
                    {"region": "华东", "amount": 3},
                    {"region": "华北", "amount": Decimal("2")},
                ],
            )
        )

    def test_preserves_duplicate_rows(self):
        self.assertFalse(results_equal([{"v": 1}, {"v": 1}], [{"v": 1}, {"v": 2}]))

    def test_ordered_topk_must_match_order(self):
        self.assertFalse(
            results_equal([{"v": 2}, {"v": 1}], [{"v": 1}, {"v": 2}], ordered=True)
        )

    def test_null_is_not_zero_or_empty(self):
        self.assertFalse(results_equal([{"v": 0}], [{"v": None}]))
        self.assertFalse(results_equal([{"v": ""}], [{"v": None}]))

    def test_missing_columns_or_result_is_failure(self):
        self.assertFalse(results_equal(None, []))
        self.assertFalse(results_equal([{"v": 1}], [{"a": 1, "b": 2}]))

    def test_money_tolerance_and_boolean_handling(self):
        self.assertFalse(results_equal([{"v": 1.02}], [{"v": 1}]))
        self.assertFalse(results_equal([{"v": True}], [{"v": 1}]))

    def test_tolerance_matching_is_not_greedy(self):
        self.assertTrue(
            results_equal([{"v": 0.01}, {"v": 0}], [{"v": 0.005}, {"v": 0.02}])
        )


if __name__ == "__main__":
    unittest.main()
