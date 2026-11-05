import unittest

from benchmark.metrics import classification_metrics, pair_metrics, retrieval_metrics


class BenchmarkMetricTests(unittest.TestCase):
    def test_pair_counts_and_denominators(self):
        result = pair_metrics([True, True, False, False], [True, False, True, False])
        self.assertEqual(
            (result["tp"], result["fp"], result["fn"], result["tn"]), (1, 1, 1, 1)
        )
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 0.5)
        self.assertEqual(result["f1"], 0.5)
        self.assertEqual(result["false_merge_fraction"], 0.5)
        self.assertEqual(result["missed_repeat_fraction"], 0.5)

    def test_no_predicted_pairs_is_not_perfect_precision(self):
        result = pair_metrics([True, False], [False, False])
        self.assertEqual(result["f1"], 0.0)
        self.assertIsNone(result["false_merge_fraction"])
        self.assertEqual(result["missed_repeat_fraction"], 1.0)
        self.assertEqual(pair_metrics([], [])["pair_count"], 0)
        with self.assertRaises(ValueError):
            pair_metrics([True], [])

    def test_confusion_and_abstention_are_separate(self):
        result = classification_metrics(
            ["bug", "bug", "other"], ["bug", "unknown", "bug"]
        )
        self.assertEqual(result["confusion_matrix"]["bug"]["bug"], 1)
        self.assertEqual(result["confusion_matrix"]["bug"]["unknown"], 1)
        self.assertEqual(result["confusion_matrix"]["other"]["bug"], 1)
        self.assertAlmostEqual(result["accuracy"], 1 / 3)
        self.assertAlmostEqual(result["abstention_fraction"], 1 / 3)
        self.assertEqual(result["per_category"]["bug"]["precision"], 0.5)
        self.assertEqual(result["per_category"]["bug"]["recall"], 0.5)
        with self.assertRaises(ValueError):
            classification_metrics(["invented"], ["bug"])

    def test_retrieval_penalizes_false_matches_for_irrelevant_query(self):
        labels = {"a": "issue", "b": "issue", "c": None}
        ranked = {"a": ["b"], "b": [], "c": ["a"]}
        result = retrieval_metrics(labels, ranked, k=5)
        self.assertAlmostEqual(result["precision_at_k"], 1 / 15)
        self.assertEqual(result["returned_precision_at_k"], 0.5)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertEqual(result["queries_with_repeats"], 2)
        self.assertEqual(result["query_hit_fraction"], 0.5)

    def test_equal_rational_f1_does_not_depend_on_precision_rounding(self):
        left = pair_metrics([True, True] + [False] * 8, [True] * 10)
        right = pair_metrics(
            [True, True, False, False, False], [True, False, True, True, True]
        )
        self.assertEqual(left["f1"], right["f1"])
        self.assertAlmostEqual(left["f1"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
