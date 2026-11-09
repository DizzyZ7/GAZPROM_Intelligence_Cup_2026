import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from benchmark.artifact_io import json_bytes, read_json, sha256
from benchmark.protocol import DatasetReader
from span_eval import evaluate, freeze, verify

OPERATION = ("продлении сертификата", "продлении сертификатов", "продлении сертификата")
SYMPTOM = ("форма не закрывается", "формы не закрываются", "форма не закрывается")


def dataset(path, split):
    rows = [
        {
            "id": f"{split}-{i}",
            "product": product,
            "channel": "web",
            "text": f"При {OPERATION[i]} {SYMPTOM[i]}.",
        }
        for i, product in enumerate(("Север", "Север", "Юг"))
    ]
    labels = [
        {
            "ticket_id": row["id"],
            "product_id": row["product"],
            "source_channel": row["channel"],
            "category": "bug",
            "workflow": "certificates",
            "issue_id": "open_form",
            "gold_cluster_id": "repeat" if i < 2 else None,
            "ground_truth": {
                "operation_evidence": [OPERATION[i]],
                "symptom_evidence": [SYMPTOM[i]],
            },
        }
        for i, row in enumerate(rows)
    ]
    blobs = {
        f"{split}.inputs.json": json_bytes(rows),
        f"{split}.labels.jsonl": b"".join(
            (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in labels
        ),
    }
    path.mkdir()
    for name, data in blobs.items():
        (path / name).write_bytes(data)
    (path / "manifest.json").write_bytes(
        json_bytes(
            {
                "schema_version": 1,
                "counts": {split: len(rows)},
                "files": {
                    name: {"sha256": sha256(data), "bytes": len(data)}
                    for name, data in blobs.items()
                },
            }
        )
    )
    return rows


def propose(text):
    for operation, symptom in zip(OPERATION, SYMPTOM):
        if f"{operation} {symptom}" in text:
            return {
                "category": "bug",
                "operation_quote": operation,
                "symptom_quote": symptom,
            }
    raise AssertionError("Неожиданный вход модели")


class SpanEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.reference = root / "reference"
        self.holdout = root / "holdout"
        self.output = root / "report"
        self.sources = dataset(self.reference, "validation")
        dataset(self.holdout, "holdout")
        self.provider = Mock()
        self.provider.describe.return_value = {
            "model": "qwen3.5:4b",
            "digest": "0" * 64,
            "ollama_version": "test",
        }
        self.provider.propose.side_effect = propose
        freeze(self.reference, self.holdout, self.output, self.provider)

    def run_split(self, split):
        return evaluate(self.reference, self.holdout, self.output, split, self.provider)

    def test_same_inference_drives_both_pairing_methods_before_gold(self):
        original = DatasetReader.read_split

        def read(reader, split, gold=True):
            self.assertEqual(self.provider.propose.call_count, 3 if gold else 0)
            return original(reader, split, gold=gold)

        with patch.object(DatasetReader, "read_split", read):
            manifest = self.run_split("validation")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(
            [call.args for call in self.provider.propose.call_args_list],
            [(row["text"],) for row in self.sources],
        )
        reports = {
            method: read_json(self.output / "validation" / method / "report.json")
            for method in ("literal", "normalized")
        }
        self.assertEqual(reports["literal"]["clustering"]["tp"], 0)
        self.assertEqual(reports["normalized"]["clustering"]["tp"], 1)
        self.assertEqual(reports["normalized"]["clustering"]["fp"], 0)
        diagnostic = read_json(self.output / "validation/diagnostic.json")
        self.assertEqual(diagnostic["fragment_quality"]["admitted_both_hit"], 3)
        self.assertEqual(diagnostic["gold_quotes_only"]["normalized"]["tp"], 1)

    def test_holdout_cannot_start_before_validation(self):
        with self.assertRaisesRegex(ValueError, "validation"):
            self.run_split("holdout")
        self.provider.propose.assert_not_called()

    def test_missing_inference_cannot_be_promoted_to_complete(self):
        self.provider.propose.side_effect = [
            propose(self.sources[0]["text"]),
            URLError("local"),
            propose(self.sources[2]["text"]),
        ]
        manifest = self.run_split("validation")
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(
            read_json(self.output / "validation/normalized/report.json")["status"],
            "partial",
        )
        manifest["status"] = "complete"
        (self.output / "validation/manifest.json").write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "Статус"):
            self.run_split("holdout")

    def test_report_verifier_detects_corrupt_evidence(self):
        for split in ("validation", "holdout"):
            self.run_split(split)
        self.assertEqual(
            verify(self.reference, self.holdout, self.output)["holdout"], "complete"
        )
        evidence = self.output / "holdout/evidence.json"
        evidence.write_text("[]\n")
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            verify(self.reference, self.holdout, self.output)

    def test_model_digest_is_checked_again_before_gold(self):
        old = self.provider.describe.return_value
        self.provider.describe.side_effect = [old, dict(old, digest="1" * 64)]
        with self.assertRaisesRegex(ValueError, "Модель"):
            self.run_split("validation")
        self.assertFalse((self.output / "validation").exists())

    def test_all_contract_invalid_proposals_are_not_checked(self):
        self.provider.propose.side_effect = None
        self.provider.propose.return_value = {
            "category": "unknown",
            "operation_quote": "лишняя цитата",
            "symptom_quote": "",
        }
        self.assertEqual(self.run_split("validation")["status"], "not_checked")

    def test_verifier_checks_frozen_gold_bytes(self):
        for split in ("validation", "holdout"):
            self.run_split(split)
        (self.holdout / "holdout.labels.jsonl").write_text("corrupt\n")
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            verify(self.reference, self.holdout, self.output)

    def test_existing_holdout_reservation_blocks_second_inference(self):
        self.run_split("validation")
        self.provider.propose.reset_mock()
        (self.output / "holdout-receipt.json").touch()
        with self.assertRaises(FileExistsError):
            self.run_split("holdout")
        self.provider.propose.assert_not_called()
