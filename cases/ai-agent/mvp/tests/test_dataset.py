import copy
import csv
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dataset") is None:
            raise AssertionError("Отсутствует воспроизводимый генератор dataset")
        from dataset.generator import generate_dataset

        cls.generate = staticmethod(generate_dataset)
        cls.bundle = generate_dataset()

    def test_default_size_and_split_counts(self):
        self.assertEqual(self.bundle["size"], 500)
        self.assertEqual(
            {s: len(d["inputs"]) for s, d in self.bundle["splits"].items()},
            {"development": 300, "validation": 100, "holdout": 100},
        )

    def test_seed_reproducibility(self):
        self.assertEqual(self.bundle, self.generate())
        self.assertNotEqual(self.bundle, self.generate(seed=17))

    def test_committed_dataset_matches_generator(self):
        from dataset.validation import load_dataset

        committed = (
            Path(__file__).resolve().parents[1] / "datasets" / "pulse-synthetic-v1"
        )
        self.assertEqual(load_dataset(committed), self.bundle)

    def test_scalable_sizes_have_exact_counts(self):
        from dataset.validation import validate_dataset

        for size in (1000, 5000, 10000):
            with self.subTest(size=size):
                bundle = self.generate(size=size)
                self.assertEqual(
                    sum(len(d["inputs"]) for d in bundle["splits"].values()), size
                )
                validate_dataset(bundle)

    def test_features_are_api_compatible_and_have_no_gold(self):
        from pulse import Ticket

        for data in self.bundle["splits"].values():
            for row in data["inputs"]:
                self.assertEqual(set(row), {"id", "product", "text", "channel"})
                self.assertRegex(row["id"], r"^t_[0-9a-f]{20}$")
                self.assertIsInstance(Ticket(**row), Ticket)
                self.assertNotIn("gold_cluster_id", row["text"])

    def test_no_family_cluster_or_text_leak_between_splits(self):
        seen_families, seen_clusters, seen_texts = set(), set(), set()
        for data in self.bundle["splits"].values():
            families = {r["ground_truth"]["split_group_id"] for r in data["labels"]}
            clusters = {
                r["gold_cluster_id"] for r in data["labels"] if r["gold_cluster_id"]
            }
            texts = {" ".join(r["text"].casefold().split()) for r in data["inputs"]}
            self.assertFalse(seen_families & families)
            self.assertFalse(seen_clusters & clusters)
            self.assertFalse(seen_texts & texts)
            seen_families.update(families)
            seen_clusters.update(clusters)
            seen_texts.update(texts)

    def test_all_required_case_types_and_annotation_fields(self):
        labels = [r for d in self.bundle["splits"].values() for r in d["labels"]]
        tags = {tag for row in labels for tag in row["ground_truth"]["case_types"]}
        self.assertTrue(
            {
                "repeated_bug",
                "feature_request",
                "no_signal",
                "cross_product",
                "lexical_hard_negative",
                "colloquial",
                "typo",
                "ambiguous",
                "multi_issue",
                "call_transcript",
                "attachment_text",
                "synthetic_pii",
                "prompt_injection",
                "rare_singleton",
            }.issubset(tags)
        )
        for row in labels:
            self.assertTrue(
                {
                    "ticket_id",
                    "product_id",
                    "category",
                    "workflow",
                    "issue_id",
                    "gold_cluster_id",
                    "normalized_intent",
                    "source_channel",
                    "expected_signal",
                    "difficulty",
                    "security_test_type",
                    "ground_truth",
                }.issubset(row)
            )
            self.assertEqual(row["ground_truth"]["expert_status"], "pending")
        for row in labels:
            if "multi_issue" in row["ground_truth"]["case_types"]:
                self.assertFalse(row["expected_signal"])
                self.assertIsNone(row["gold_cluster_id"])
                self.assertGreaterEqual(len(row["ground_truth"]["issues"]), 2)
            if "rare_singleton" in row["ground_truth"]["case_types"]:
                self.assertFalse(row["expected_signal"])

    def test_events_replay_and_revise_without_label_features(self):
        from dataset.validation import validate_dataset

        validate_dataset(self.bundle)
        actions = set()
        for data in self.bundle["splits"].values():
            self.assertGreater(len(data["events"]), len(data["inputs"]))
            for event, label in zip(data["events"], data["event_labels"]):
                self.assertNotIn("expected_action", event)
                self.assertNotIn("category", event)
                self.assertEqual(event["event_id"], label["event_id"])
                actions.add(label["expected_action"])
        self.assertEqual(actions, {"insert", "ignore_duplicate", "update"})

    def test_validator_rejects_gold_in_feature_and_broken_labels(self):
        from dataset.validation import validate_dataset

        for mutation in ("gold", "product", "signal", "revision", "split"):
            broken = copy.deepcopy(self.bundle)
            data = broken["splits"]["development"]
            if mutation == "gold":
                data["inputs"][0]["category"] = "bug"
            elif mutation == "product":
                data["labels"][0]["product_id"] = "Другой продукт"
            elif mutation == "signal":
                positive = next(r for r in data["labels"] if r["expected_signal"])
                positive["gold_cluster_id"] = None
            elif mutation == "revision":
                data["events"][0]["revision"] = 5
            else:
                broken["splits"]["validation"]["labels"][0]["ground_truth"][
                    "split_group_id"
                ] = data["labels"][0]["ground_truth"]["split_group_id"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_dataset(broken)

    def test_files_checksums_blind_review_and_no_overwrite(self):
        from dataset.generator import write_dataset
        from dataset.validation import load_dataset

        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "a", Path(temp) / "b"
            write_dataset(self.bundle, first)
            write_dataset(self.generate(), second)
            files = {p.name: p.read_bytes() for p in first.iterdir()}
            self.assertEqual(files, {p.name: p.read_bytes() for p in second.iterdir()})
            self.assertEqual(load_dataset(first), self.bundle)
            manifest = json.loads(files["manifest.json"])
            for name, details in manifest["files"].items():
                self.assertEqual(
                    hashlib.sha256(files[name]).hexdigest(), details["sha256"]
                )
            review = list(
                csv.DictReader(files["expert-review.csv"].decode().splitlines())
            )
            self.assertEqual(len(review), 40)
            self.assertTrue(all(not row["reviewer_category"] for row in review))
            self.assertNotIn("gold_cluster_id", review[0])
            holdout_ids = {
                row["id"] for row in self.bundle["splits"]["holdout"]["inputs"]
            }
            self.assertFalse(holdout_ids & {r["ticket_id"] for r in review})
            with self.assertRaises(FileExistsError):
                write_dataset(self.bundle, first)
            (first / "holdout.inputs.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_dataset(first)

    def test_cli_generate_validate_and_reject_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            target = str(Path(temp) / "dataset")
            generated = subprocess.run(
                [sys.executable, "-m", "dataset", "generate", "--output", target],
                capture_output=True,
                text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            checked = subprocess.run(
                [sys.executable, "-m", "dataset", "validate", "--directory", target],
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
        for kwargs in ({"size": 0}, {"size": True}, {"size": 100.5}, {"seed": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.generate(**kwargs)


if __name__ == "__main__":
    unittest.main()
