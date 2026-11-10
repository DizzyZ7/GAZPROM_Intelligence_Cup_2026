import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from benchmark.artifact_io import json_bytes, read_json, sha256
from benchmark.protocol import DatasetReader
from quote_candidates_eval import _gold, _saved_span_input, calibrate, evaluate, verify

OPERATION = "При продлении сертификата"
SYMPTOM = "форма не закрывается"


def dataset(path):
    path.mkdir()
    files = {}
    for split in ("validation", "holdout"):
        rows = [
            {
                "id": f"{split}-{i}",
                "product": product,
                "channel": "web",
                "text": f"{OPERATION} {SYMPTOM}. Пример {split}.",
            }
            for i, product in enumerate(("Север", "Север", "Юг"))
        ]
        labels = [
            {
                "ticket_id": row["id"],
                "product_id": row["product"],
                "source_channel": row["channel"],
                "category": "bug",
                "issue_id": "closed_form",
                "workflow": "certificates",
                "gold_cluster_id": "repeat" if i < 2 else None,
                "ground_truth": {"must_abstain": False},
            }
            for i, row in enumerate(rows)
        ]
        quotes = [
            {
                "ticket_id": row["id"],
                "category": "bug",
                "operation_quote": OPERATION,
                "symptom_quote": SYMPTOM,
            }
            for row in rows
        ]
        files[f"{split}.inputs.json"] = json_bytes(rows)
        files[f"{split}.evidence.json"] = json_bytes(quotes)
        files[f"{split}.labels.jsonl"] = b"".join(
            (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in labels
        )
    for name, data in files.items():
        (path / name).write_bytes(data)
    (path / "manifest.json").write_bytes(
        json_bytes(
            {
                "schema_version": 1,
                "counts": {"validation": 3, "holdout": 3},
                "files": {
                    name: {"sha256": sha256(data), "bytes": len(data)}
                    for name, data in files.items()
                },
            }
        )
    )


class QuoteSearchEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.data = root / "data"
        self.output = root / "report"
        dataset(self.data)
        self.provider = Mock()
        self.provider.embed.side_effect = lambda texts: [[1.0, 0.0] for _ in texts]
        self.describe = Mock(
            return_value={
                "model": "bge-m3:567m",
                "digest": "0" * 64,
                "ollama_version": "test",
            }
        )

    def test_validation_freezes_threshold_and_holdout_predicts_before_gold(self):
        original = DatasetReader.read_split
        observed = []

        def read(reader, split, gold=True):
            if gold:
                observed.append((split, self.provider.embed.call_count))
            return original(reader, split, gold=gold)

        with patch.object(DatasetReader, "read_split", read):
            config = calibrate(self.data, self.output, self.provider, self.describe)
            result = evaluate(
                self.data, self.output, "holdout", self.provider, self.describe
            )
        self.assertEqual(observed, [("validation", 1), ("holdout", 2)])
        self.assertEqual(config["threshold"], 0.95)
        self.assertEqual(result["methods"]["semantic"]["candidate_pairs"]["tp"], 1)
        self.assertEqual(result["methods"]["semantic"]["candidate_pairs"]["fp"], 0)
        self.assertEqual(result["methods"]["normalized"]["candidate_pairs"]["tp"], 1)
        self.assertEqual(verify(self.data, self.output)["holdout"], "verified")
        csv = (self.output / "holdout/candidates.csv").read_text()
        self.assertIn("pending_human_review", csv)
        self.assertNotIn("holdout-2", csv)

    def test_model_drift_and_second_holdout_are_rejected(self):
        calibrate(self.data, self.output, self.provider, self.describe)
        original = self.describe.return_value
        self.describe.return_value = dict(original, digest="1" * 64)
        with self.assertRaisesRegex(ValueError, "Модель"):
            evaluate(self.data, self.output, "holdout", self.provider, self.describe)
        self.assertFalse((self.output / "holdout-receipt.json").exists())
        self.describe.return_value = original
        evaluate(self.data, self.output, "holdout", self.provider, self.describe)
        with self.assertRaises(FileExistsError):
            evaluate(self.data, self.output, "holdout", self.provider, self.describe)

    def test_verifier_rejects_changed_quoted_input_and_gold_bytes(self):
        calibrate(self.data, self.output, self.provider, self.describe)
        evaluate(self.data, self.output, "holdout", self.provider, self.describe)
        evidence = self.data / "holdout.evidence.json"
        original = evidence.read_bytes()
        evidence.write_bytes(b"[]")
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            verify(self.data, self.output)
        evidence.write_bytes(original)
        (self.data / "holdout.labels.jsonl").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            verify(self.data, self.output)

    def test_changed_report_is_rejected_before_any_holdout_inference(self):
        calibrate(self.data, self.output, self.provider, self.describe)
        report = self.output / "validation/report.json"
        obj = read_json(report)
        obj["threshold"] = 0.5
        report.write_bytes(json_bytes(obj))
        self.provider.embed.reset_mock()
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            evaluate(self.data, self.output, "holdout", self.provider, self.describe)
        self.provider.embed.assert_not_called()

    def test_existing_holdout_receipt_rejects_before_reading_holdout(self):
        calibrate(self.data, self.output, self.provider, self.describe)
        (self.output / "holdout-receipt.json").touch()
        original = DatasetReader.read_split

        def read(reader, split, gold=True):
            self.assertEqual(split, "validation")
            return original(reader, split, gold=gold)

        with (
            patch.object(DatasetReader, "read_split", read),
            self.assertRaises(FileExistsError),
        ):
            evaluate(self.data, self.output, "holdout", self.provider, self.describe)

    def test_retrospective_uses_the_published_pr9_ledger_without_gold(self):
        original = DatasetReader.read_split

        def read(reader, split, gold=True):
            self.assertFalse(gold)
            return original(reader, split, gold=False)

        with patch.object(DatasetReader, "read_split", read):
            _, sources, proposals, provenance = _saved_span_input()
        self.assertEqual(len(sources), 36)
        self.assertEqual(len(proposals), 11)
        self.assertEqual(len(provenance["span_manifest_sha256"]), 64)
        with (
            patch("quote_candidates_eval._SPAN_MANIFEST_SHA256", "0" * 64),
            self.assertRaisesRegex(ValueError, "опубликованного PR"),
        ):
            _saved_span_input()

    def test_repeated_service_questions_are_not_product_gold_pairs(self):
        rows = [
            {
                "ticket_id": identity,
                "category": "how_to",
                "gold_cluster_id": "repeated_question",
                "product_id": "Север",
            }
            for identity in ("q1", "q2")
        ]
        self.assertEqual(_gold(rows), {"q1": None, "q2": None})
        self.assertTrue(
            all(row["gold_cluster_id"] == "repeated_question" for row in rows)
        )
