"""Воспроизводимая оценка контекстного анализа по отдельным выборкам."""

import argparse
import json
import platform
import time
from collections import Counter
from functools import partial
from itertools import combinations
from pathlib import Path

from benchmark.artifact_io import (
    csv_bytes,
    json_bytes,
    read_json,
    sha256,
    write_directory,
)
from benchmark.engine import MAX_PAIRS
from benchmark.metrics import classification_metrics, pair_metrics
from benchmark.protocol import DatasetReader, source_fingerprints
from pipeline import analyze_contextual
from pulse import Ticket, analyze, redact
from ticket_context import ANALYSIS_VERSION

VERSION = "pulse-context-evaluation-v1"
MIN_TICKETS = 2
SPLITS = ("development", "validation", "holdout")
ANALYZERS = {
    "rules": partial(analyze, threshold=MIN_TICKETS),
    "context": partial(analyze_contextual, min_tickets=MIN_TICKETS),
}
ARTIFACTS = {
    "frozen-config.json",
    "summary.json",
    "predictions.csv",
    "comparison.csv",
    "receipt.json",
}
ERROR_LIMIT = 15


def _configuration(reader, split):
    if split not in SPLITS:
        raise ValueError("Неизвестная выборка")
    count = reader.manifest.get("counts", {}).get(split)
    if type(count) is not int or count < 1 or count * (count - 1) // 2 > MAX_PAIRS:
        raise ValueError("Недопустимый размер контрольной выборки")
    sources = source_fingerprints()
    for name in ("context_eval.py", "pipeline.py", "ticket_context.py"):
        sources[name] = sha256(Path(__file__).with_name(name).read_bytes())
    return {
        "version": VERSION,
        "split": split,
        "dataset_version": reader.manifest["dataset_version"],
        "dataset_manifest_sha256": reader.fingerprint,
        "sources": sources,
        "methods": {
            "rules": {"min_tickets": MIN_TICKETS},
            "context": {"version": ANALYSIS_VERSION, "min_tickets": MIN_TICKETS},
        },
    }


def freeze(dataset_dir, config_path, *, split="holdout"):
    config = _configuration(DatasetReader(dataset_dir), split)
    with Path(config_path).open("xb") as stream:
        stream.write(json_bytes(config))
    return config


def _score(analysis, labels, elapsed):
    gold = {row["ticket_id"]: row for row in labels}
    predicted = {row["id"]: row for row in analysis["classified"]}
    if set(predicted) != set(gold) or len(analysis["classified"]) != len(gold):
        raise ValueError("Результат классификации не соответствует выборке")
    clustered = {}
    for signal in analysis["signals"]:
        for identity in signal["ticket_ids"]:
            if identity not in gold or identity in clustered:
                raise ValueError("Участники групп не соответствуют выборке")
            clustered[identity] = signal["id"]

    ids = sorted(gold)
    gold_pairs, predicted_pairs = [], []
    false_merges, missed_repeats = [], []
    cross_product = 0
    for left, right in combinations(ids, 2):
        group = gold[left]["gold_cluster_id"]
        true_pair = group is not None and group == gold[right]["gold_cluster_id"]
        predicted_pair = left in clustered and clustered[left] == clustered.get(right)
        gold_pairs.append(true_pair)
        predicted_pairs.append(predicted_pair)
        if predicted_pair:
            cross_product += gold[left]["product_id"] != gold[right]["product_id"]
        if predicted_pair and not true_pair and len(false_merges) < ERROR_LIMIT:
            false_merges.append([left, right])
        if true_pair and not predicted_pair and len(missed_repeats) < ERROR_LIMIT:
            missed_repeats.append([left, right])

    rows = [
        {
            "ticket_id": identity,
            "product": gold[identity]["product_id"],
            "case_type": gold[identity].get("case_type"),
            "gold_category": gold[identity]["category"],
            "predicted_category": predicted[identity]["category"],
            "predicted_workflow": predicted[identity]["workflow"],
            "gold_cluster": gold[identity]["gold_cluster_id"],
            "predicted_cluster": clustered.get(identity),
            "operation": predicted[identity].get("operation"),
            "symptom": predicted[identity].get("symptom"),
            "reason": predicted[identity].get("reason"),
        }
        for identity in ids
    ]
    instructions = [
        row
        for row in rows
        if row["case_type"] == "embedded_instruction"
        or "prompt_injection"
        in (gold[row["ticket_id"]].get("security_test_type") or "").split("+")
    ]
    resolved_groups = Counter(
        (row["product"], row["gold_cluster"])
        for row in rows
        if row["reason"] == "context_resolved" and row["gold_cluster"] is not None
    )
    return {
        "classification": classification_metrics(
            [row["gold_category"] for row in rows],
            [row["predicted_category"] for row in rows],
        ),
        "clustering": pair_metrics(gold_pairs, predicted_pairs),
        "signals": len(analysis["signals"]),
        "cross_product_pairs": cross_product,
        "resolved_gold_pairs": (
            sum(count * (count - 1) // 2 for count in resolved_groups.values())
            if any("reason" in row for row in predicted.values())
            else None
        ),
        "instruction_cases": len(instructions),
        "instruction_cases_detected": sum(
            row["reason"] == "untrusted_instruction" for row in instructions
        ),
        "instruction_cases_in_clusters": sum(
            row["predicted_cluster"] is not None for row in instructions
        ),
        "reasons": dict(Counter(row["reason"] for row in rows if row["reason"])),
        "analysis_seconds": elapsed,
        "tickets_per_second": len(rows) / elapsed if elapsed else None,
        "errors": {
            "false_merges": false_merges,
            "missed_repeats": missed_repeats,
            "wrong_categories": [
                row["ticket_id"]
                for row in rows
                if row["gold_category"] != row["predicted_category"]
            ][:ERROR_LIMIT],
        },
    }, rows


def evaluate(dataset_dir, config_path, output):
    output, config_path = Path(output), Path(config_path)
    if output.exists():
        raise FileExistsError("Отчет уже существует")
    reader = DatasetReader(dataset_dir)
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config.get("dataset_manifest_sha256") != reader.fingerprint:
        raise ValueError("Набор отличается от зафиксированного")
    split = config.get("split")
    if config != _configuration(reader, split):
        raise ValueError("Код или параметры изменены после фиксации")
    run = {"configuration_sha256": sha256(config_bytes)}
    receipt = config_path.with_name(f"{split}-{reader.fingerprint}.receipt.json")
    with receipt.open("xb") as stream:
        stream.write(json_bytes(dict(run, status="started")))

    try:
        sources, _ = reader.read_split(split, gold=False)
        tickets = [Ticket(**dict(row, text=redact(row["text"]))) for row in sources]
        results = {}
        for method, analyzer in ANALYZERS.items():
            start = time.perf_counter()
            analysis = analyzer(tickets)
            results[method] = (analysis, time.perf_counter() - start)
        _, labels = reader.read_split(split)

        reports, predictions, comparisons = {}, [], []
        for method, (analysis, elapsed) in results.items():
            report, rows = _score(analysis, labels, elapsed)
            reports[method] = report
            predictions.extend(dict(row, method=method) for row in rows)
            comparisons.append(
                {
                    "method": method,
                    "category_macro_f1": report["classification"]["macro_f1"],
                    "category_accuracy": report["classification"]["accuracy"],
                    "abstention_fraction": report["classification"][
                        "abstention_fraction"
                    ],
                    **report["clustering"],
                    "signals": report["signals"],
                    "cross_product_pairs": report["cross_product_pairs"],
                    "resolved_gold_pairs": report["resolved_gold_pairs"],
                    "instruction_cases_detected": report["instruction_cases_detected"],
                    "instruction_cases_in_clusters": report[
                        "instruction_cases_in_clusters"
                    ],
                    "analysis_seconds": elapsed,
                    "tickets_per_second": report["tickets_per_second"],
                }
            )
        summary = {
            "version": VERSION,
            "split": split,
            "dataset_version": config["dataset_version"],
            "tickets": len(tickets),
            "python": platform.python_version(),
            "methods": reports,
        }
        summary_bytes = json_bytes(summary)
        completed = json_bytes(
            dict(run, status="completed", summary_sha256=sha256(summary_bytes))
        )
        files = {
            "frozen-config.json": config_bytes,
            "summary.json": summary_bytes,
            "predictions.csv": csv_bytes(list(predictions[0]), predictions),
            "comparison.csv": csv_bytes(list(comparisons[0]), comparisons),
            "receipt.json": completed,
        }
        files["manifest.json"] = json_bytes(
            {
                "version": VERSION,
                "files": {
                    name: {"bytes": len(content), "sha256": sha256(content)}
                    for name, content in files.items()
                },
            }
        )
        write_directory(output, files)
        receipt.write_bytes(completed)
    except Exception:
        receipt.write_bytes(json_bytes(dict(run, status="failed")))
        raise
    return summary


def verify(directory, dataset_dir):
    root = Path(directory)
    manifest = read_json(root / "manifest.json")
    if (
        manifest.get("version") != VERSION
        or set(manifest.get("files", {})) != ARTIFACTS
    ):
        raise ValueError("Неизвестный формат отчета")
    for name, expected in manifest["files"].items():
        content = (root / name).read_bytes()
        if expected != {"bytes": len(content), "sha256": sha256(content)}:
            raise ValueError(f"Поврежден артефакт {name}")
    config = read_json(root / "frozen-config.json")
    receipt = read_json(root / "receipt.json")
    reader = DatasetReader(dataset_dir)
    if config["dataset_manifest_sha256"] != reader.fingerprint:
        raise ValueError("Манифест набора отличается от зафиксированного")
    if receipt != {
        "status": "completed",
        "configuration_sha256": sha256((root / "frozen-config.json").read_bytes()),
        "summary_sha256": sha256((root / "summary.json").read_bytes()),
    }:
        raise ValueError("Квитанция не соответствует отчету")
    split = config.get("split")
    current = _configuration(reader, split)
    sources, _ = reader.read_split(split)
    return {
        "status": "ok",
        "split": split,
        "tickets": len(sources),
        "source_matches_current": config == current,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("freeze", "evaluate", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", required=True)
        if name == "freeze":
            command.add_argument("--split", choices=SPLITS, default="holdout")
        if name != "verify":
            command.add_argument("--config", required=True)
        if name != "freeze":
            command.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze(args.dataset, args.config, split=args.split)
        elif args.command == "evaluate":
            result = evaluate(args.dataset, args.config, args.output)
        else:
            result = verify(args.output, args.dataset)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
