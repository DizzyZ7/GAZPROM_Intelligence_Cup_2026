import math
import unittest

from benchmark.engine import analyze_method, apply_threshold
from benchmark.tfidf import TfidfModel
from pulse import Ticket, analyze


class FixedProvider:
    def __init__(self, vectors):
        self.vectors, self.seen = vectors, []

    def embed(self, texts):
        self.seen.extend(texts)
        return self.vectors


class BenchmarkEngineTests(unittest.TestCase):
    def test_tfidf_unknown_terms_cannot_change_vocabulary(self):
        model = TfidfModel.fit(["кот кот пес", "пес"])
        before = model.to_state()
        vector = model.transform(["экскаватор"])[0]
        self.assertEqual(model.to_state(), before)
        self.assertEqual(vector, {})
        self.assertAlmostEqual(model.to_state()["idf"]["w1:кот"], 1 + math.log(3 / 2))
        same = model.transform(["кот кот пес", "кот кот пес"])
        self.assertAlmostEqual(sum(x * x for x in same[0].values()), 1.0)
        self.assertEqual(same[0], same[1])
        self.assertEqual(TfidfModel.from_state(before).to_state(), before)

    def test_word_and_character_branches_are_both_present(self):
        vector = TfidfModel.fit(["бронирование очереди"]).transform(
            ["бронирование очереди"]
        )[0]
        self.assertTrue(any(key.startswith("w2:") for key in vector))
        self.assertTrue(any(key.startswith("c3:") for key in vector))
        self.assertAlmostEqual(
            sum(v * v for k, v in vector.items() if k.startswith("w")), 0.5
        )
        self.assertAlmostEqual(
            sum(v * v for k, v in vector.items() if k.startswith("c")), 0.5
        )

    def test_rules_groups_equal_existing_baseline(self):
        tickets = [
            Ticket("a", "P", "Очередь ошибка 404"),
            Ticket("b", "P", "Бронирование ошибка 404"),
            Ticket("c", "Q", "Очередь ошибка 404"),
            Ticket("d", "P", "Привет"),
        ]
        actual = analyze_method("rules", tickets)
        expected = {frozenset(s["ticket_ids"]) for s in analyze(tickets)["signals"]}
        self.assertEqual(
            {frozenset(g) for g in actual["groups"] if len(g) > 1}, expected
        )

    def test_embeddings_use_predicted_context_and_never_cross_products(self):
        tickets = [
            Ticket("a", "P", "Очередь ошибка 404 a@example.invalid"),
            Ticket("b", "Q", "Очередь ошибка 404"),
            Ticket("c", "P", "Восстановить пароль код смс"),
            Ticket("d", "P", "Привет"),
        ]
        provider = FixedProvider([[1.0, 0.0]] * 4)
        result = analyze_method("embeddings", tickets, threshold=0.5, provider=provider)
        self.assertFalse(any(p["matched"] for p in result["pairs"]))
        self.assertNotIn("a@example.invalid", " ".join(provider.seen))
        self.assertEqual(result["predictions"][-1]["category"], "unknown")

    def test_complete_link_does_not_join_a_similarity_chain(self):
        tickets = [Ticket(c, "P", "Очередь ошибка 404") for c in "abc"]
        # AB=BC=.8, AC=.28: один кластер из трех недопустим.
        provider = FixedProvider([[1, 0], [0.8, 0.6], [0.28, 0.96]])
        result = analyze_method("embeddings", tickets, threshold=0.7, provider=provider)
        self.assertEqual(sorted(len(g) for g in result["groups"]), [1, 2])
        self.assertEqual(sum(p["matched"] for p in result["pairs"]), 2)
        self.assertEqual(sum(p["clustered"] for p in result["pairs"]), 1)

    def test_invalid_embeddings_and_pair_budget_fail_closed(self):
        tickets = [
            Ticket("a", "P", "Очередь ошибка 404"),
            Ticket("b", "P", "Очередь ошибка 404"),
        ]
        for vectors in (
            [],
            [[0, 0], [1, 0]],
            [[True, 0], [1, 0]],
            [[float("nan")], [1]],
            [[1], [1, 0]],
        ):
            with self.subTest(vectors=vectors), self.assertRaises(ValueError):
                analyze_method(
                    "embeddings",
                    tickets,
                    threshold=0.5,
                    provider=FixedProvider(vectors),
                )
        with self.assertRaises(ValueError):
            analyze_method("rules", tickets, max_pairs=0)
        with self.assertRaises(ValueError):
            analyze_method(
                "tfidf",
                tickets,
                threshold=float("nan"),
                tfidf=TfidfModel.fit(["очередь"]),
            )

    def test_threshold_search_reuses_scores_without_mutating_original(self):
        tickets = [Ticket(c, "P", "Очередь ошибка 404") for c in "ab"]
        provider = FixedProvider([[1, 0], [0.8, 0.6]])
        base = analyze_method("embeddings", tickets, threshold=0.5, provider=provider)
        strict = apply_threshold(base, 0.9)
        self.assertEqual([len(g) for g in base["groups"]], [2])
        self.assertEqual([len(g) for g in strict["groups"]], [1, 1])
        self.assertEqual(len(provider.seen), 2)
        self.assertIsNone(strict["performance"])

    def test_cluster_identifiers_cannot_collide_on_embedded_separators(self):
        tickets = [
            Ticket("a", "P", "Очередь ошибка 404"),
            Ticket("b\nc", "P", "Очередь ошибка 404"),
            Ticket("a\nb", "Q", "Очередь ошибка 404"),
            Ticket("c", "Q", "Очередь ошибка 404"),
        ]
        result = analyze_method("rules", tickets)
        self.assertEqual(sum(p["matched"] for p in result["pairs"]), 2)
        self.assertEqual(sum(p["clustered"] for p in result["pairs"]), 2)


if __name__ == "__main__":
    unittest.main()
