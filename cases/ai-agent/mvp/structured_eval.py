"""Зафиксированный офлайн-эксперимент с локальным извлечением контекста."""

import argparse
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

from benchmark.artifact_io import json_bytes, read_json, sha256, write_directory
from benchmark.engine import _attach_groups, _context, analyze_method
from benchmark.protocol import DatasetReader, source_fingerprints
from benchmark.reports import comparison_files, method_files
from pulse import Ticket
from structured_context import (
    MODEL,
    OPTIONS,
    PROMPT,
    VERSION,
    OllamaContextProvider,
    extract_context,
)
from ticket_context import ACTIONABLE, sanitized_text

SAMPLE_COUNTS = {"development": 14, "validation": 24}
SAMPLE_SEED = 20261109
MAX_HOLDOUT = 64
SLOT_MIN_F1 = 0.5
_SPLITS = (*SAMPLE_COUNTS, "holdout")
_METHODS = ("rules_v2_3", "structured")
_METHOD_FILES = (
    "report.json",
    "performance.json",
    "errors.json",
    "groups.json",
    "predictions.csv",
    "confusion.csv",
    "retrieval.csv",
)
_REPORT_FILES = {"comparison.csv", "evidence.json"} | {
    f"{method}/{name}" for method in _METHODS for name in _METHOD_FILES
}


def _configuration(reference, holdout):
    files = [Path(__file__), Path(__file__).with_name("structured_context.py")]
    return {
        "version": VERSION,
        "sources": source_fingerprints()
        | {p.name: sha256(p.read_bytes()) for p in files},
        "reference_manifest_sha256": reference.fingerprint,
        "holdout_manifest_sha256": holdout.fingerprint,
        "sample_counts": SAMPLE_COUNTS,
        "sample_seed": SAMPLE_SEED,
        "prompt_sha256": sha256(PROMPT.encode()),
        "options": OPTIONS,
        "slot_min_f1": SLOT_MIN_F1,
        "pairing": "exact_product_category_operation_symptom",
    }


def freeze(reference_dir, holdout_dir, output, provider):
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    if reference.fingerprint == holdout.fingerprint:
        raise ValueError("Требуется отдельный контрольный набор")
    if not 0 < holdout.manifest["counts"]["holdout"] <= MAX_HOLDOUT:
        raise ValueError("Превышен размер контрольной выборки")
    config = _configuration(reference, holdout)
    write_directory(
        output,
        {"frozen-config.json": json_bytes(dict(config, model=provider.describe()))},
    )


def _load_config(reference, holdout, root):
    data = (root / "frozen-config.json").read_bytes()
    config = read_json(root / "frozen-config.json")
    if {
        key: value for key, value in config.items() if key != "model"
    } != _configuration(reference, holdout):
        raise ValueError("Код, параметры или наборы отличаются от фиксации")
    return config, sha256(data)


def _select(sources, split):
    if split in SAMPLE_COUNTS:
        sources = sorted(
            sources,
            key=lambda row: sha256(f"{SAMPLE_SEED}:{split}:{row['id']}".encode()),
        )[: SAMPLE_COUNTS[split]]
    return sorted(sources, key=lambda row: row["id"])


def _analyze(tickets, provider):
    start = time.perf_counter()
    rows, evidence = [], []
    for ticket in tickets:
        item = extract_context(ticket.text, provider.propose)
        context = item.context
        rows.append(
            {
                "ticket_id": ticket.id,
                "product": ticket.product,
                "category": context.category,
                "workflow": context.workflow,
                "classifier_score": None,
                "operation": context.operation,
                "symptom": context.symptom,
                "reason": context.reason,
                "groupable": context.groupable,
            }
        )
        evidence.append(
            {
                "ticket_id": ticket.id,
                "sanitized_text_sha256": sha256(sanitized_text(ticket.text).encode()),
                "model_called": item.model_called,
                "operation_span": item.operation_span,
                "symptom_span": item.symptom_span,
                "reason": context.reason,
            }
        )
    classified = time.perf_counter()
    pairs = []
    for left, right in combinations(rows, 2):
        reason = _context(left, right)
        matched = reason == "context_pass"
        pairs.append(
            {
                "left": left["ticket_id"],
                "right": right["ticket_id"],
                "score": 1.0 if matched else None,
                "matched": matched,
                "reason": "structured_key_match" if matched else reason,
            }
        )
    groups = _attach_groups(rows, pairs)
    elapsed = time.perf_counter() - start
    return {
        "method": "structured",
        "classifier": VERSION,
        "threshold": None,
        "predictions": rows,
        "pairs": pairs,
        "groups": groups,
        "performance": {
            "total_seconds": elapsed,
            "classification_seconds": classified - start,
            "tickets_per_second": len(tickets) / elapsed,
            "python_peak_bytes": None,
            "ticket_count": len(tickets),
            "pair_count": len(pairs),
        },
    }, evidence


def _slot_hit(source, span, alternatives):
    if span is None:
        return False
    left, right = span
    for raw in alternatives:
        quote = sanitized_text(raw)
        if not quote:
            continue
        start = source.find(quote)
        while start >= 0:
            overlap = max(0, min(right, start + len(quote)) - max(left, start))
            if 2 * overlap / (right - left + len(quote)) >= SLOT_MIN_F1:
                return True
            start = source.find(quote, start + 1)
    return False


def _grounded_slots(sources, labels, evidence):
    truth = {row["ticket_id"]: row.get("ground_truth", {}) for row in labels}
    source_by_id = {row["id"]: row["text"] for row in sources}
    counts = {"labeled": 0, "operation_hit": 0, "symptom_hit": 0, "both_hit": 0}
    rejected, violations = 0, []
    for item in evidence:
        identity = item["ticket_id"]
        annotation = truth[identity]
        operation = annotation.get("operation_evidence")
        symptom = annotation.get("symptom_evidence")
        if operation and symptom:
            source = source_by_id[identity]
            op_hit = _slot_hit(source, item["operation_span"], operation)
            symptom_hit = _slot_hit(source, item["symptom_span"], symptom)
            counts["labeled"] += 1
            counts["operation_hit"] += op_hit
            counts["symptom_hit"] += symptom_hit
            counts["both_hit"] += op_hit and symptom_hit
        if annotation.get("must_abstain") is True:
            rejected += 1
            if item["operation_span"] is not None:
                violations.append(identity)
    return dict(
        counts, must_abstain_cases=rejected, accepted_despite_abstention=violations
    )


def _verify_split(root, split, config_sha, reader):
    directory = root / split
    manifest = read_json(directory / "manifest.json")
    files = manifest.get("files", {})
    inputs, _ = reader.read_split(split, gold=False)
    if (
        manifest.get("selected_inputs_sha256")
        != sha256(json_bytes(_select(inputs, split)))
        or manifest.get("split") != split
        or manifest.get("status") not in {"complete", "partial", "not_checked"}
        or manifest.get("configuration_sha256") != config_sha
        or manifest.get("dataset_manifest_sha256") != reader.fingerprint
        or set(files) != _REPORT_FILES
    ):
        raise ValueError("Отчет не соответствует зафиксированному эксперименту")
    for name, digest in files.items():
        if sha256((directory / name).read_bytes()) != digest:
            raise ValueError(f"Не совпадает контрольная сумма {split}/{name}")
    report = read_json(directory / "structured/report.json")
    if report.get("inference_status") != manifest["status"]:
        raise ValueError("Статус модели отличается от манифеста")
    return manifest["status"]


def _check_gold(labels):
    scopes = {}
    for row in labels:
        group = row["gold_cluster_id"]
        if group is None:
            continue
        scope = (row["product_id"], row["category"])
        if (
            row["category"] not in ACTIONABLE
            or scopes.setdefault(group, scope) != scope
        ):
            raise ValueError("Нарушена область эталонного кластера")


def evaluate(reference_dir, holdout_dir, output, split, provider):
    if split not in _SPLITS:
        raise ValueError("Неизвестная выборка")
    root = Path(output)
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    config, config_sha = _load_config(reference, holdout, root)
    target = root / split
    if target.exists():
        raise FileExistsError("Выборка уже оценена по этой фиксации")
    if split == "holdout":
        if (
            not (root / "validation/manifest.json").is_file()
            or _verify_split(root, "validation", config_sha, reference) != "complete"
        ):
            raise ValueError("Перед holdout требуется завершенная оценка validation")
    if provider.describe() != config["model"]:
        raise ValueError("Модель отличается от зафиксированной")
    reader = holdout if split == "holdout" else reference
    inputs, _ = reader.read_split(split, gold=False)
    sources = _select(inputs, split)
    tickets = [Ticket(**row) for row in sources]
    baseline = analyze_method("rules", tickets, classifier="context")
    structured, evidence = _analyze(tickets, provider)
    if provider.describe() != config["model"]:
        raise ValueError("Модель изменилась во время оценки")
    if _load_config(reference, holdout, root)[1] != config_sha:
        raise ValueError("Фиксация изменилась во время оценки")

    # В том числе holdout: эталоны читаются только после обеих серий предсказаний.
    _, all_labels = reader.read_split(split)
    identities = {row["id"] for row in sources}
    labels = [row for row in all_labels if row["ticket_id"] in identities]
    _check_gold(labels)
    safe_sources = [dict(row, text=sanitized_text(row["text"])) for row in sources]
    files, reports, performances = {}, {}, {}
    reasons = Counter(row["reason"] for row in evidence)
    calls = sum(row["model_called"] for row in evidence)
    incomplete = sum(
        reasons[reason]
        for reason in ("provider_unavailable", "invalid_response", "invalid_schema")
    )
    status = (
        "not_checked"
        if calls == incomplete
        else "partial"
        if incomplete
        else "complete"
    )
    for name, analysis in zip(_METHODS, (baseline, structured)):
        additions, report = method_files(name, analysis, safe_sources, labels)
        if name == "structured":
            report.update(
                grounded_slots=_grounded_slots(safe_sources, labels, evidence),
                model=config["model"],
                inference_status=status,
                status="ok" if status == "complete" else status,
                model_calls=calls,
                decision_reasons=dict(reasons),
            )
            additions[f"{name}/report.json"] = json_bytes(report)
        files.update(additions)
        reports[name], performances[name] = report, analysis["performance"]
    files["comparison.csv"] = comparison_files(reports, performances, split=split)[
        "comparison.csv"
    ]
    files["evidence.json"] = json_bytes(evidence)
    manifest = {
        "split": split,
        "status": status,
        "configuration_sha256": config_sha,
        "dataset_manifest_sha256": reader.fingerprint,
        "selected_inputs_sha256": sha256(json_bytes(sources)),
        "files": {name: sha256(data) for name, data in files.items()},
    }
    write_directory(target, files | {"manifest.json": json_bytes(manifest)})
    return manifest


def verify(reference_dir, holdout_dir, output):
    root = Path(output)
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    _, config_sha = _load_config(reference, holdout, root)
    return {
        split: _verify_split(
            root, split, config_sha, holdout if split == "holdout" else reference
        )
        for split in _SPLITS
        if split != "development" or (root / split).exists()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "evaluate", "verify"))
    parser.add_argument("--reference", required=True)
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--split", choices=_SPLITS)
    args = parser.parse_args()
    common = (args.reference, args.holdout, args.output)
    try:
        if args.command == "verify":
            result = verify(*common)
        elif args.command == "freeze":
            freeze(*common, OllamaContextProvider(args.model))
            result = read_json(Path(args.output) / "frozen-config.json")["model"]
        elif args.split is None:
            parser.error("Для evaluate требуется --split")
        else:
            result = evaluate(*common, args.split, OllamaContextProvider(args.model))
            result = {
                key: result[key] for key in ("split", "status", "configuration_sha256")
            }
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json_bytes(result).decode(), end="")


if __name__ == "__main__":
    main()
