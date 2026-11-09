import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.artifact_io import sha256
from dataset.generator import generate_dataset, write_dataset


class DatasetReviewTests(unittest.TestCase):
    def test_rehashed_fabricated_packet_is_rejected_before_gold(self):
        from dataset.review import ANNOTATIONS, assess_review, prepare_review

        prepare_review(self.dataset, self.root / "packet", count=10)
        packet = self.root / "packet" / "review.csv"
        with packet.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["text"] = "Подмененное доказательство"
        with packet.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        manifest_path = packet.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_bytes())
        manifest["review_csv_sha256"] = sha256(packet.read_bytes())
        manifest_path.write_text(json.dumps(manifest))
        for row in rows:
            row.update(dict.fromkeys(ANNOTATIONS, ""))
            row.update(reviewer_category="other", reviewer_workflow="general")
        completed = self.root / "completed.csv"
        with completed.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        original = Path.read_bytes

        def guarded(path):
            if path.name.endswith("labels.jsonl"):
                raise AssertionError("Поврежденный пакет открыл gold")
            return original(path)

        with patch.object(Path, "read_bytes", guarded), self.assertRaises(ValueError):
            assess_review(self.dataset, packet.parent, completed, reviewer="unit-test")

    def test_header_only_rehashed_packet_is_rejected(self):
        from dataset.review import assess_review, prepare_review

        prepare_review(self.dataset, self.root / "packet", count=10)
        packet = self.root / "packet" / "review.csv"
        packet.write_text(packet.read_text().splitlines()[0] + "\n")
        manifest_path = packet.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_bytes())
        manifest.update(
            count=0, ticket_ids=[], review_csv_sha256=sha256(packet.read_bytes())
        )
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            assess_review(self.dataset, packet.parent, packet, reviewer="unit-test")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.dataset = self.root / "dataset"
        write_dataset(generate_dataset(size=100, seed=41), self.dataset)

    def test_blind_packet_never_reads_gold_or_holdout_and_is_reproducible(self):
        from dataset.review import prepare_review

        original = Path.read_bytes

        def guarded(path):
            if path.name.endswith("labels.jsonl") or path.name.startswith("holdout."):
                raise AssertionError("Слепой пакет прочитал эталоны или holdout")
            return original(path)

        with patch.object(Path, "read_bytes", guarded):
            prepare_review(self.dataset, self.root / "packet", count=20, seed=7)
            prepare_review(self.dataset, self.root / "packet2", count=20, seed=7)
        content = (self.root / "packet" / "review.csv").read_bytes()
        self.assertEqual(content, (self.root / "packet2" / "review.csv").read_bytes())
        rows = list(csv.DictReader(content.decode().splitlines()))
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(row["reviewer_category"] == "" for row in rows))
        self.assertNotIn("gold_cluster_id", rows[0])

    def test_incomplete_or_altered_review_is_rejected(self):
        from dataset.review import assess_review, prepare_review

        prepare_review(self.dataset, self.root / "packet", count=10)
        with self.assertRaises(ValueError):
            assess_review(
                self.dataset,
                self.root / "packet",
                self.root / "packet" / "review.csv",
                reviewer="test",
            )

    def test_submitted_review_reports_disagreement_without_rewriting_gold(self):
        from dataset.review import assess_review, prepare_review

        prepare_review(self.dataset, self.root / "packet", count=10)
        source = self.root / "packet" / "review.csv"
        with source.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row.update(
                reviewer_category="other",
                reviewer_workflow="general",
                reviewer_cluster_id="",
                reviewer_comment="Тестовая независимая разметка",
            )
        completed = self.root / "completed.csv"
        with completed.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        before = (self.dataset / "development.labels.jsonl").read_bytes()
        report = assess_review(
            self.dataset, self.root / "packet", completed, reviewer="unit-test"
        )
        self.assertEqual(report["reviewed_tickets"], 10)
        self.assertGreater(len(report["disagreements"]), 0)
        self.assertEqual(
            before, (self.dataset / "development.labels.jsonl").read_bytes()
        )
        rows[0]["text"] = "Замененное доказательство"
        with completed.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaises(ValueError):
            assess_review(
                self.dataset, self.root / "packet", completed, reviewer="unit-test"
            )
