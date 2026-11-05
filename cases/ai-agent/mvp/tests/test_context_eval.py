import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.artifact_io import json_bytes, sha256
from context_eval import evaluate, freeze, verify


class ContextEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "dataset"
        self.dataset.mkdir()
        self.config = self.root / "frozen.json"
        self.output = self.root / "result"
        sources = [
            {
                "id": identity,
                "product": product,
                "text": "Очередь ошибка 404",
                "channel": "web",
            }
            for identity, product in (("a", "P"), ("b", "P"), ("c", "Q"))
        ]
        labels = [
            {
                "ticket_id": source["id"],
                "product_id": source["product"],
                "source_channel": "web",
                "category": "bug",
                "workflow": "booking_queue",
                "issue_id": "code404",
                "gold_cluster_id": "P-code404" if source["product"] == "P" else None,
                "expected_signal": source["product"] == "P",
                "case_type": "lexical_overlap",
            }
            for source in sources
        ]
        self.inputs = json_bytes(sources)
        self.labels = b"".join(
            json_bytes(row).replace(b"\n", b" ") + b"\n" for row in labels
        )
        self.files = {
            "holdout.inputs.json": self.inputs,
            "holdout.labels.jsonl": self.labels,
        }
        self.write_manifest()

    def write_manifest(self):
        (self.dataset / "manifest.json").write_bytes(
            json_bytes(
                {
                    "schema_version": 1,
                    "dataset_version": "fixture",
                    "counts": {"holdout": 3},
                    "files": {
                        name: {"bytes": len(value), "sha256": sha256(value)}
                        for name, value in self.files.items()
                    },
                }
            )
        )

    def write_data(self):
        for name, data in self.files.items():
            (self.dataset / name).write_bytes(data)

    def test_freeze_uses_only_manifest_and_evaluation_is_single_use(self):
        freeze(self.dataset, self.config)
        self.write_data()
        summary = evaluate(self.dataset, self.config, self.output)
        methods = summary["methods"]
        self.assertEqual(methods["context"]["clustering"]["f1"], 1)
        self.assertEqual(methods["context"]["cross_product_pairs"], 0)
        self.assertEqual(methods["rules"]["classification"]["accuracy"], 1)
        self.assertTrue(verify(self.output, self.dataset)["source_matches_current"])
        with self.assertRaises(FileExistsError):
            evaluate(self.dataset, self.config, self.root / "other-result")
        copied_config = self.config.with_name("copied-config.json")
        copied_config.write_bytes(self.config.read_bytes())
        with self.assertRaises(FileExistsError):
            evaluate(self.dataset, copied_config, self.root / "other-result")
        (self.output / "predictions.csv").write_text("altered")
        with self.assertRaises(ValueError):
            verify(self.output, self.dataset)

    def test_changed_code_or_manifest_rejects_before_opening_holdout(self):
        freeze(self.dataset, self.config)
        with patch("context_eval.source_fingerprints", return_value={}):
            with self.assertRaisesRegex(ValueError, "Код"):
                evaluate(self.dataset, self.config, self.output)
        self.files["holdout.labels.jsonl"] += b" "
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Набор"):
            evaluate(self.dataset, self.config, self.output)

    def test_gold_is_read_after_both_predictions(self):
        freeze(self.dataset, self.config)
        self.write_data()
        calls = []
        from benchmark.protocol import DatasetReader
        from context_eval import ANALYZERS

        read = DatasetReader.read_split

        def track_read(reader, split, gold=True):
            calls.append("gold" if gold else "inputs")
            return read(reader, split, gold=gold)

        def track_analyzer(name, analyzer):
            def wrapped(tickets):
                calls.append(name)
                return analyzer(tickets)

            return wrapped

        with (
            patch.object(DatasetReader, "read_split", track_read),
            patch.dict(
                ANALYZERS,
                {
                    name: track_analyzer(name, analyzer)
                    for name, analyzer in ANALYZERS.items()
                },
            ),
        ):
            evaluate(self.dataset, self.config, self.output)
        self.assertEqual(calls, ["inputs", "rules", "context", "gold"])


if __name__ == "__main__":
    unittest.main()
