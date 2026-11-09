import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from benchmark.artifact_io import json_bytes, sha256
from benchmark.protocol import DatasetReader
from structured_eval import evaluate, freeze, verify

TEXT = "При выдаче пропуска окно закрывается без сохранения."
PROPOSAL = {
    "category": "bug",
    "operation": "выдача пропуска",
    "symptom": "закрытие без сохранения",
    "operation_quote": "выдаче пропуска",
    "symptom_quote": "окно закрывается без сохранения",
}


def make_dataset(path, split):
    sources = [
        {"id": str(i), "product": product, "text": TEXT, "channel": "web"}
        for i, product in enumerate(("Север", "Север", "Юг"))
    ]
    labels = [
        {
            "ticket_id": row["id"],
            "product_id": row["product"],
            "source_channel": row["channel"],
            "category": "bug",
            "workflow": "pass_issue",
            "issue_id": "form_dismissed",
            "gold_cluster_id": "one" if row["product"] == "Север" else None,
            "ground_truth": {
                "operation_evidence": ["выдаче пропуска"],
                "symptom_evidence": ["окно закрывается без сохранения"],
            },
        }
        for row in sources
    ]
    files = {
        f"{split}.inputs.json": json_bytes(sources),
        f"{split}.labels.jsonl": b"".join(
            (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in labels
        ),
    }
    path.mkdir()
    for name, data in files.items():
        (path / name).write_bytes(data)
    (path / "manifest.json").write_bytes(
        json_bytes(
            {
                "schema_version": 1,
                "counts": {split: len(sources)},
                "files": {
                    name: {"sha256": sha256(data), "bytes": len(data)}
                    for name, data in files.items()
                },
            }
        )
    )


class StructuredEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.reference = root / "reference"
        self.holdout = root / "holdout"
        self.output = root / "output"
        make_dataset(self.reference, "validation")
        make_dataset(self.holdout, "holdout")
        self.provider = Mock()
        self.provider.describe.return_value = {
            "model": "qwen3.5:4b",
            "digest": "0" * 64,
            "ollama_version": "test",
        }
        self.provider.propose.return_value = PROPOSAL
        freeze(self.reference, self.holdout, self.output, self.provider)

    def test_holdout_requires_completed_validation(self):
        with self.assertRaisesRegex(ValueError, "validation"):
            evaluate(
                self.reference, self.holdout, self.output, "holdout", self.provider
            )
        self.provider.propose.assert_not_called()

    def test_evaluation_keeps_gold_out_of_inference_and_products_isolated(self):
        original = DatasetReader.read_split

        def read(reader, split, gold=True):
            self.assertEqual(self.provider.propose.call_count, 3 if gold else 0)
            return original(reader, split, gold=gold)

        with patch.object(DatasetReader, "read_split", read):
            result = evaluate(
                self.reference, self.holdout, self.output, "validation", self.provider
            )
        self.assertEqual(result["status"], "complete")
        report = json.loads(
            (self.output / "validation/structured/report.json").read_bytes()
        )
        self.assertEqual(report["clustering"]["tp"], 1)
        self.assertEqual(report["clustering"]["fp"], 0)
        self.assertEqual(report["grounded_slots"]["both_hit"], 3)
        self.assertTrue(
            all(call.args == (TEXT,) for call in self.provider.propose.call_args_list)
        )
        with self.assertRaises(FileExistsError):
            evaluate(
                self.reference, self.holdout, self.output, "validation", self.provider
            )

    def test_verify_detects_corrupted_evidence(self):
        for split in ("validation", "holdout"):
            evaluate(self.reference, self.holdout, self.output, split, self.provider)
        self.assertEqual(
            verify(self.reference, self.holdout, self.output)["holdout"], "complete"
        )
        evidence = self.output / "holdout/evidence.json"
        evidence.write_text("[]\n")
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            verify(self.reference, self.holdout, self.output)

    def test_digest_change_disqualifies_run(self):
        old = self.provider.describe.return_value
        self.provider.describe.side_effect = [old, dict(old, digest="1" * 64)]
        with self.assertRaisesRegex(ValueError, "Модель"):
            evaluate(
                self.reference, self.holdout, self.output, "validation", self.provider
            )
        self.assertFalse((self.output / "validation").exists())

    def test_incomplete_model_run_cannot_be_marked_complete_in_manifest(self):
        self.provider.propose.side_effect = [PROPOSAL, PROPOSAL, URLError("local test")]
        result = evaluate(
            self.reference, self.holdout, self.output, "validation", self.provider
        )
        self.assertEqual(result["status"], "partial")
        report = json.loads(
            (self.output / "validation/structured/report.json").read_bytes()
        )
        self.assertEqual(report["status"], "partial")
        self.assertIn(
            "structured,partial,",
            (self.output / "validation/comparison.csv").read_text(),
        )
        result["status"] = "complete"
        (self.output / "validation/manifest.json").write_bytes(json_bytes(result))
        with self.assertRaisesRegex(ValueError, "Статус"):
            evaluate(
                self.reference, self.holdout, self.output, "holdout", self.provider
            )

    def test_validation_manifest_is_bound_to_selected_inputs(self):
        result = evaluate(
            self.reference, self.holdout, self.output, "validation", self.provider
        )
        result["selected_inputs_sha256"] = "0" * 64
        (self.output / "validation/manifest.json").write_bytes(json_bytes(result))
        with self.assertRaisesRegex(ValueError, "Отчет"):
            evaluate(
                self.reference, self.holdout, self.output, "holdout", self.provider
            )

    def test_schema_invalid_answers_mark_model_unchecked(self):
        self.provider.propose.return_value = {}
        result = evaluate(
            self.reference, self.holdout, self.output, "validation", self.provider
        )
        self.assertEqual(result["status"], "not_checked")
