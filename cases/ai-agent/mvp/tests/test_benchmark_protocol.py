import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.protocol import calibrate, evaluate, verify_report
from dataset.generator import generate_dataset, write_dataset


class BenchmarkProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset"
        self.calibration = self.root / "calibration"
        self.output = self.root / "result"
        write_dataset(generate_dataset(size=100, seed=41), self.dataset)

    def tearDown(self):
        self.temporary.cleanup()

    def test_calibration_never_reads_holdout(self):
        original = Path.read_bytes

        def checked(path, *args, **kwargs):
            if path.name.startswith("holdout."):
                raise AssertionError("Калибровка прочитала holdout")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_bytes", checked):
            frozen = calibrate(self.dataset, self.calibration)
        self.assertEqual(frozen["selection_split"], "validation")
        self.assertEqual(frozen["methods"]["embeddings"]["status"], "not_checked")
        self.assertIn(frozen["methods"]["tfidf"]["threshold"], frozen["threshold_grid"])
        self.assertTrue((self.calibration / "validation-grid.csv").is_file())

    def test_report_separates_tasks_and_missing_model(self):
        calibrate(self.dataset, self.calibration)
        summary = evaluate(self.dataset, self.calibration, self.output)
        self.assertEqual(summary["status"], "partial")
        report = json.loads((self.output / "rules" / "report.json").read_text())
        self.assertTrue(
            {
                "classification",
                "retrieval_pairs",
                "retrieval_top_k",
                "clustering",
            }.issubset(report)
        )
        self.assertNotEqual(
            report["classification"].keys(), report["clustering"].keys()
        )
        missing = json.loads((self.output / "embeddings" / "report.json").read_text())
        self.assertEqual(missing["status"], "not_checked")
        self.assertIsNone(missing["classification"])
        self.assertIsNone(missing["clustering"])
        self.assertIn("не проверено", (self.output / "comparison.md").read_text())
        self.assertTrue((self.output / "tfidf" / "predictions.csv").is_file())
        self.assertTrue((self.output / "tfidf" / "confusion.csv").is_file())
        self.assertTrue((self.output / "tfidf" / "errors.json").is_file())
        self.assertEqual(verify_report(self.output)["status"], "valid")

    def test_protocol_and_state_tampering_are_rejected(self):
        calibrate(self.dataset, self.calibration)
        config_file = self.calibration / "frozen-config.json"
        original = config_file.read_bytes()
        broken = json.loads(original)
        broken["methods"]["tfidf"]["threshold"] = 0.99
        config_file.write_text(json.dumps(broken))
        with self.assertRaises(ValueError):
            evaluate(self.dataset, self.calibration, self.output)
        config_file.write_bytes(original)
        (self.calibration / "tfidf-state.json").write_text("{}")
        with self.assertRaises(ValueError):
            evaluate(self.dataset, self.calibration, self.output)
        self.assertFalse((self.calibration / "holdout-receipt.json").exists())

    def test_holdout_replay_is_blocked_and_saved_report_damage_detected(self):
        calibrate(self.dataset, self.calibration)
        evaluate(self.dataset, self.calibration, self.output)
        with self.assertRaises(FileExistsError):
            evaluate(self.dataset, self.calibration, self.root / "second")
        (self.output / "tfidf" / "report.json").write_text("{}")
        with self.assertRaises(ValueError):
            verify_report(self.output)

    def test_changed_dataset_is_not_evaluated_with_old_threshold(self):
        calibrate(self.dataset, self.calibration)
        manifest = self.dataset / "manifest.json"
        changed = json.loads(manifest.read_bytes())
        changed["seed"] = 42
        manifest.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            evaluate(self.dataset, self.calibration, self.output)

    def test_no_gold_can_reach_the_method_inputs(self):
        from benchmark import protocol

        original = protocol.analyze_method
        seen = []

        def checked(method, tickets, **kwargs):
            self.assertTrue(
                all(
                    set(vars(ticket)) == {"id", "product", "text", "channel"}
                    for ticket in tickets
                )
            )
            seen.append(method)
            return original(method, tickets, **kwargs)

        with patch.object(protocol, "analyze_method", checked):
            calibrate(self.dataset, self.calibration)
            evaluate(self.dataset, self.calibration, self.output)
        self.assertTrue({"rules", "tfidf"}.issubset(seen))

    def test_embedding_contract_and_model_revision_are_enforced(self):
        class Provider:
            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        descriptor = {
            "provider": "unit-test-fixture",
            "model": "constant-vectors",
            "revision": "fixture-1",
        }
        provider = Provider()
        config = calibrate(self.dataset, self.calibration, provider, descriptor)
        self.assertEqual(config["methods"]["embeddings"]["status"], "ready")
        for candidate in (None, dict(descriptor, revision="fixture-2")):
            with self.assertRaises(ValueError):
                evaluate(
                    self.dataset, self.calibration, self.output, provider, candidate
                )
        self.assertFalse((self.calibration / "holdout-receipt.json").exists())
        result = evaluate(
            self.dataset, self.calibration, self.output, provider, descriptor
        )
        self.assertEqual(result["methods"]["embeddings"], "ok")

    def test_model_failure_is_explicit_and_does_not_echo_source_text(self):
        class Broken:
            def embed(self, texts):
                raise RuntimeError("do-not-log-ticket-body")

        descriptor = {
            "provider": "unit-test-fixture",
            "model": "broken",
            "revision": "fixture-1",
        }
        config = calibrate(self.dataset, self.calibration, Broken(), descriptor)
        self.assertEqual(config["methods"]["embeddings"]["status"], "failed")
        result = evaluate(self.dataset, self.calibration, self.output)
        self.assertEqual(result["methods"]["embeddings"], "failed")
        self.assertNotIn(
            "do-not-log-ticket-body",
            (self.output / "embeddings" / "report.json").read_text(),
        )

    def test_reader_rejects_gold_even_with_valid_file_checksums(self):
        feature_file = self.dataset / "development.inputs.json"
        rows = json.loads(feature_file.read_bytes())
        rows[0]["gold_cluster_id"] = "forbidden-feature"
        content = json.dumps(rows, ensure_ascii=False).encode()
        feature_file.write_bytes(content)
        manifest_file = self.dataset / "manifest.json"
        manifest = json.loads(manifest_file.read_bytes())
        manifest["files"][feature_file.name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            calibrate(self.dataset, self.calibration)

    def test_verification_requires_complete_task_evidence(self):
        calibrate(self.dataset, self.calibration)
        evaluate(self.dataset, self.calibration, self.output)
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_bytes())
        del manifest["files"]["tfidf/confusion.csv"]
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            verify_report(self.output)

    def test_cli_cannot_change_threshold_during_holdout(self):
        commands = [
            ["tune", "--dataset", str(self.dataset), "--output", str(self.calibration)],
            [
                "evaluate",
                "--dataset",
                str(self.dataset),
                "--calibration",
                str(self.calibration),
                "--output",
                str(self.output),
            ],
            ["verify", "--directory", str(self.output)],
        ]
        result = subprocess.run(
            [sys.executable, "-m", "benchmark", *commands[0]],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        forbidden = subprocess.run(
            [sys.executable, "-m", "benchmark", *commands[1], "--threshold", "0.5"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(forbidden.returncode, 2)
        self.assertFalse((self.calibration / "holdout-receipt.json").exists())
        for command in commands[1:]:
            result = subprocess.run(
                [sys.executable, "-m", "benchmark", *command],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_embedding_adapter_change_invalidates_frozen_source(self):
        calibrate(self.dataset, self.calibration)
        original = Path.read_bytes

        def changed(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            return (
                result + b"\n# different transport"
                if path.name == "ollama_embeddings.py"
                else result
            )

        with patch.object(Path, "read_bytes", changed), self.assertRaises(ValueError):
            evaluate(self.dataset, self.calibration, self.output)

    def test_manifest_cannot_misstate_split_dataset_or_completeness(self):
        calibrate(self.dataset, self.calibration)
        evaluate(self.dataset, self.calibration, self.output)
        path = self.output / "manifest.json"
        original = json.loads(path.read_bytes())
        for field, value in (
            ("status", "complete"),
            ("evaluation_split", "development"),
            ("dataset_manifest_sha256", "0" * 64),
        ):
            modified = dict(original, **{field: value})
            path.write_text(json.dumps(modified))
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_report(self.output)
        path.write_text(json.dumps(original))

    def test_zero_true_positive_calibration_is_explicit(self):
        fixture = self.root / "no-matches"
        fixture.mkdir()
        manifest = {
            "schema_version": 1,
            "dataset_version": "unit-fixture",
            "counts": {},
            "files": {},
        }
        for split, count in (("development", 1), ("validation", 2), ("holdout", 2)):
            sources = [
                {
                    "id": f"{split}-{i}",
                    "product": "P",
                    "text": "Загрузка профиля завершается кодом E77",
                    "channel": "web",
                }
                for i in range(count)
            ]
            labels = [
                {
                    "ticket_id": r["id"],
                    "product_id": "P",
                    "category": "bug",
                    "workflow": "profile_loading",
                    "issue_id": "profile-load",
                    "gold_cluster_id": "P|profile-load",
                    "source_channel": "web",
                }
                for r in sources
            ]
            for name, value in (
                (split + ".inputs.json", json.dumps(sources).encode()),
                (
                    split + ".labels.jsonl",
                    b"".join(json.dumps(r).encode() + b"\n" for r in labels),
                ),
            ):
                (fixture / name).write_bytes(value)
                manifest["files"][name] = {
                    "sha256": hashlib.sha256(value).hexdigest(),
                    "bytes": len(value),
                }
            manifest["counts"][split] = count
        (fixture / "manifest.json").write_text(json.dumps(manifest))
        frozen = calibrate(fixture, self.calibration)
        selection = frozen["methods"]["tfidf"]
        self.assertEqual(selection["calibration_status"], "no_true_positives")
        self.assertEqual(selection["gold_positive_pairs"], 1)
        self.assertEqual(selection["best_validation_tp"], 0)
        self.assertEqual(selection["best_validation_f1"], 0.0)

    def test_threshold_tie_prefers_fewer_false_merges_using_exact_counts(self):
        from itertools import combinations

        from benchmark.protocol import _select

        sources = [
            {"id": c, "product": "P", "text": "Очередь ошибка 404", "channel": "web"}
            for c in "abcde"
        ]
        labels = [
            {
                "ticket_id": c,
                "category": "bug",
                "issue_id": cluster or "single",
                "gold_cluster_id": cluster,
            }
            for c, cluster in zip("abcde", ["X", "X", "Y", "Y", None])
        ]
        predictions = [
            {
                "ticket_id": c,
                "category": "bug",
                "workflow": "booking_queue",
                "product": "P",
            }
            for c in "abcde"
        ]
        pairs = [
            {
                "left": a,
                "right": b,
                "score": 0.8
                if ({a, b} <= {"a", "b", "c"} or {a, b} <= {"d", "e"})
                else 0.4,
                "matched": False,
                "reason": "below_threshold",
            }
            for a, b in combinations("abcde", 2)
        ]
        result = _select(
            {
                "method": "tfidf",
                "threshold": 0.4,
                "predictions": predictions,
                "pairs": pairs,
                "performance": None,
            },
            sources,
            labels,
        )
        self.assertEqual(result[0], 0.8)

    def test_empty_gold_cluster_is_rejected_instead_of_creating_a_repeat(self):
        path = self.dataset / "validation.labels.jsonl"
        labels = [json.loads(line) for line in path.read_bytes().splitlines()]
        labels[0]["gold_cluster_id"] = ""
        content = b"".join(json.dumps(row).encode() + b"\n" for row in labels)
        path.write_bytes(content)
        manifest_path = self.dataset / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"][path.name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            calibrate(self.dataset, self.calibration)


if __name__ == "__main__":
    unittest.main()
