"""Калибровка и проверка поиска кандидатов на независимых выборках."""

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from benchmark.artifact_io import (
    csv_bytes,
    json_bytes,
    read_json,
    sha256,
    write_directory,
)
from benchmark.metrics import pair_metrics, retrieval_metrics
from benchmark.protocol import DatasetReader, local_ollama, source_fingerprints
from quote_candidates import TOP_K, VERSION, bind_evidence, rank_candidates, score_pairs
from span_context import safe_text
from span_eval import _load_config as _load_span_config
from span_eval import _verify_split as _verify_span_split
from structured_context import OllamaContextProvider
from structured_eval import _check_gold
from ticket_context import ACTIONABLE

MODEL = "bge-m3:567m"
GRID = tuple(i / 100 for i in range(50, 96, 5))
MIN_PRECISION = 0.8
METHODS = ("normalized", "semantic")
_ROOT = Path(__file__).resolve().parent
_SPAN_REPORT = _ROOT / "benchmarks/pulse-span-context-v1"
_SPAN_DATA = _ROOT / "datasets/pulse-span-holdout-v1"
_SPAN_REFERENCE = _ROOT / "datasets/pulse-synthetic-v1"
# SHA-256 опубликованных входов PR №9, merge f8ac4d9.
_SPAN_CONFIG_SHA256 = "9cf9268de99cd79a85c00343c74fa8625bac7ee7283b0098ae03f154520402d3"
_SPAN_MANIFEST_SHA256 = (
    "30c2735ba7492dc5fc3ce46b50919930af377976e83b4bfd1aee141c8b698f11"
)
_SPAN_DATA_SHA256 = "b5c759f80e5302dcaab48ef12fa1fcb27fffff2acdcd08133814bda7ff0e82d9"
_REPORT_FILES = {
    "report.json",
    "comparison.csv",
    "candidates.csv",
    "evidence.json",
    "errors.json",
}
_CANDIDATE_FIELDS = (
    "method",
    "query",
    "neighbor",
    "rank",
    "product",
    "category",
    "operation_similarity",
    "symptom_similarity",
    "score",
    "status",
)


def _configuration(reader):
    paths = (
        "quote_candidates.py",
        "quote_candidates_eval.py",
        "span_context.py",
        "span_eval.py",
        "structured_context.py",
        "structured_eval.py",
    )
    return {
        "version": VERSION,
        "dataset_manifest_sha256": reader.fingerprint,
        "sources": source_fingerprints()
        | {name: sha256((_ROOT / name).read_bytes()) for name in paths},
        "normalizer": version("snowballstemmer"),
        "validation_grid": list(GRID),
        "minimum_validation_precision": MIN_PRECISION,
        "top_k": TOP_K,
        "gold_target": "actionable_product_repeats",
        "score": "min(operation_cosine,symptom_cosine)",
    }


def _input(reader, split):
    sources, _ = reader.read_split(split, gold=False)
    name = f"{split}.evidence.json"
    payload = (reader.root / name).read_bytes()
    expected = reader.manifest["files"].get(name, {})
    digest = sha256(payload)
    if digest != expected.get("sha256") or len(payload) != expected.get("bytes"):
        raise ValueError("Контрольная сумма подтвержденных фрагментов не совпадает")
    proposals = json.loads(payload)
    if not isinstance(proposals, list):
        raise ValueError("Неверный формат подтвержденных фрагментов")
    return (
        sources,
        proposals,
        {
            "dataset_manifest_sha256": reader.fingerprint,
            "inputs_sha256": sha256(json_bytes(sources)),
            "evidence_sha256": digest,
        },
    )


def _saved_span_input():
    reference, reader = DatasetReader(_SPAN_REFERENCE), DatasetReader(_SPAN_DATA)
    _, fingerprint = _load_span_config(_SPAN_REPORT, reference, reader)
    split = "holdout"
    directory = _SPAN_REPORT / split
    manifest_bytes = (directory / "manifest.json").read_bytes()
    if (
        fingerprint != _SPAN_CONFIG_SHA256
        or sha256(manifest_bytes) != _SPAN_MANIFEST_SHA256
        or reader.fingerprint != _SPAN_DATA_SHA256
    ):
        raise ValueError("Вход ретроспективы отличается от опубликованного PR №9")
    if _verify_span_split(_SPAN_REPORT, split, fingerprint, reader) != "complete":
        raise ValueError("Исходный прогон Qwen не завершен")
    sources, _ = reader.read_split(split, gold=False)
    by_id = {row["id"]: row for row in sources}
    ledger = (directory / "evidence.json").read_bytes()
    ledger_sha = sha256(ledger)
    if ledger_sha != json.loads(manifest_bytes)["files"]["evidence.json"]:
        raise ValueError("Изменились сохраненные фрагменты Qwen")
    proposals = []
    for item in json.loads(ledger):
        if not item["admitted"]:
            continue
        identity = item["ticket_id"]
        text = safe_text(by_id[identity]["text"])
        proposals.append(
            {
                "ticket_id": identity,
                "category": item["category"],
                "operation_quote": text[slice(*item["operation_span"])],
                "symptom_quote": text[slice(*item["symptom_span"])],
            }
        )
    return (
        reader,
        sources,
        proposals,
        {
            "dataset_manifest_sha256": reader.fingerprint,
            "inputs_sha256": sha256(json_bytes(sources)),
            "evidence_sha256": ledger_sha,
            "span_configuration_sha256": fingerprint,
            "span_manifest_sha256": _SPAN_MANIFEST_SHA256,
        },
    )


def _score(sources, proposals, provider):
    started = time.perf_counter()
    evidence, reasons = bind_evidence(sources, proposals)
    ids = sorted(reasons)
    pairs, info = score_pairs(ids, evidence, provider)
    elapsed = time.perf_counter() - started
    return (
        ids,
        evidence,
        reasons,
        pairs,
        info
        | {
            "scoring_seconds": elapsed,
            "tickets_per_second": len(ids) / elapsed,
        },
    )


def _gold(labels):
    product_labels = [
        row if row["category"] in ACTIONABLE else dict(row, gold_cluster_id=None)
        for row in labels
    ]
    _check_gold(product_labels)
    return {row["ticket_id"]: row["gold_cluster_id"] for row in product_labels}


def _pair_keys(ranked):
    return {
        tuple(sorted((query, neighbor)))
        for query, neighbors in ranked.items()
        for neighbor in neighbors
    }


def _truth(pairs, gold):
    return [
        gold[pair["left"]] is not None and gold[pair["left"]] == gold[pair["right"]]
        for pair in pairs
    ]


def _metrics(pairs, truth, ranked):
    keys = _pair_keys(ranked)
    result = pair_metrics(truth, [(p["left"], p["right"]) in keys for p in pairs])
    result["irrelevant_candidate_fraction"] = result.pop("false_merge_fraction")
    return result


def _choose_threshold(ids, pairs, truth):
    rows = []
    for threshold in GRID:
        ranked = rank_candidates(ids, pairs, "semantic", threshold)
        quality = _metrics(pairs, truth, ranked)
        rows.append({"threshold": threshold, **quality})
    eligible = [row for row in rows if row["tp"] and row["precision"] >= MIN_PRECISION]
    best = max(
        eligible,
        key=lambda row: (row["f1"], row["precision"], row["threshold"]),
        default=None,
    )
    return (best["threshold"] if best else None), rows


def _rank(ids, pairs, threshold):
    return {
        method: rank_candidates(ids, pairs, method, threshold) for method in METHODS
    }


def _report(split, scored, ranked, labels, config):
    ids, evidence, reasons, pairs, timing = scored
    gold = _gold(labels)
    truth = _truth(pairs, gold)
    by_pair = {(pair["left"], pair["right"]): pair for pair in pairs}
    exclusions = Counter(pair["reason"] for pair in pairs)
    mandatory = {
        row["ticket_id"]
        for row in labels
        if row.get("ground_truth", {}).get("must_abstain") is True
    }
    reports, errors, candidates = {}, {}, []
    for method in METHODS:
        neighbors = ranked[method]
        proposed = _pair_keys(neighbors)
        reports[method] = {
            "candidate_pairs": _metrics(pairs, truth, neighbors),
            "retrieval_top_k": retrieval_metrics(gold, neighbors, TOP_K),
        }
        false_candidates, misses = [], []
        for pair, expected in zip(pairs, truth):
            selected = (pair["left"], pair["right"]) in proposed
            if selected == expected:
                continue
            reason = pair["reason"]
            if reason == "context_pass":
                if method == "normalized":
                    reason = (
                        "top_k"
                        if pair["normalized_match"]
                        else "different_normalized_fragments"
                    )
                elif config["threshold"] is None:
                    reason = "threshold_unavailable"
                elif pair["score"] < config["threshold"]:
                    reason = "below_threshold"
                else:
                    reason = "top_k" if not selected else "similarity_pass"
            (false_candidates if selected else misses).append(dict(pair, reason=reason))
        errors[method] = {
            "false_candidates": false_candidates[:20],
            "missed_repeats": misses[:20],
            "miss_reasons": dict(Counter(pair["reason"] for pair in misses)),
        }
        for query, suggestions in neighbors.items():
            for rank, neighbor in enumerate(suggestions, 1):
                pair = by_pair[tuple(sorted((query, neighbor)))]
                item = evidence[query]
                candidates.append(
                    {
                        "method": method,
                        "query": query,
                        "neighbor": neighbor,
                        "rank": rank,
                        "product": item.product,
                        "category": item.category,
                        "operation_similarity": pair["operation_similarity"]
                        if method == "semantic"
                        else None,
                        "symptom_similarity": pair["symptom_similarity"]
                        if method == "semantic"
                        else None,
                        "score": pair["score"] if method == "semantic" else 1.0,
                        "status": "pending_human_review",
                    }
                )
    report = {
        "split": split,
        "tickets": len(ids),
        "accepted_evidence": len(evidence),
        "gold_positive_pairs": sum(truth),
        "non_product_grouped_tickets": sum(
            row["category"] not in ACTIONABLE and row["gold_cluster_id"] is not None
            for row in labels
        ),
        "guard_eligible_gold_pairs": sum(
            expected and pair["reason"] == "context_pass"
            for expected, pair in zip(truth, pairs)
        ),
        "embedding_status": "checked"
        if timing["embedded_fragments"]
        else "not_checked_no_eligible_pairs",
        "model": config["model"],
        "threshold": config["threshold"],
        "methods": reports,
        "evidence_reasons": dict(Counter(reasons.values())),
        "pair_guards": dict(exclusions),
        "mandatory_abstentions": len(mandatory),
        "accepted_despite_abstention": sorted(mandatory & evidence.keys()),
        "performance": timing,
    }
    comparison = [
        {"method": method, **value["candidate_pairs"]}
        for method, value in reports.items()
    ]
    return report, {
        "report.json": json_bytes(report),
        "comparison.csv": csv_bytes(tuple(comparison[0]), comparison),
        "candidates.csv": csv_bytes(_CANDIDATE_FIELDS, candidates),
        "evidence.json": json_bytes(
            [
                {
                    "ticket_id": identity,
                    "reason": reasons[identity],
                    "quote": asdict(evidence[identity])
                    if identity in evidence
                    else None,
                }
                for identity in ids
            ]
        ),
        "errors.json": json_bytes(errors),
    }


def _manifest(split, files, fingerprint, provenance):
    return json_bytes(
        {
            "split": split,
            "configuration_sha256": fingerprint,
            "inputs": provenance,
            "files": {name: sha256(content) for name, content in files.items()},
        }
    )


def calibrate(dataset, output, provider, describe):
    reader = DatasetReader(dataset)
    base_config = _configuration(reader)
    model = describe()
    sources, proposals, provenance = _input(reader, "validation")
    scored = _score(sources, proposals, provider)
    if model != describe() or base_config != _configuration(DatasetReader(dataset)):
        raise ValueError("Модель, код или набор изменились во время калибровки")
    _, labels = reader.read_split("validation")
    ids, _, _, pairs, _ = scored
    threshold, grid = _choose_threshold(ids, pairs, _truth(pairs, _gold(labels)))
    config = base_config | {
        "model": model,
        "threshold": threshold,
        "calibration_status": "selected"
        if threshold is not None
        else "no_qualified_threshold",
    }
    config_bytes = json_bytes(config)
    _, files = _report(
        "validation", scored, _rank(ids, pairs, threshold), labels, config
    )
    files["grid.csv"] = csv_bytes(tuple(grid[0]), grid)
    files["manifest.json"] = _manifest(
        "validation", files, sha256(config_bytes), provenance
    )
    write_directory(
        Path(output),
        {"frozen-config.json": config_bytes}
        | {f"validation/{name}": data for name, data in files.items()},
    )
    return config


def _load_config(root, reader):
    path = root / "frozen-config.json"
    data = path.read_bytes()
    config = json.loads(data)
    if {
        key: value
        for key, value in config.items()
        if key not in {"model", "threshold", "calibration_status"}
    } != _configuration(reader) or config["threshold"] not in (None, *GRID):
        raise ValueError("Код, параметры или набор отличаются от фиксации")
    return config, sha256(data)


def _verify_artifacts(root, split, config, fingerprint, provenance):
    directory = root / split
    manifest = read_json(directory / "manifest.json")
    files = manifest.get("files", {})
    expected = _REPORT_FILES | ({"grid.csv"} if split == "validation" else set())
    if (
        manifest.get("split") != split
        or manifest.get("configuration_sha256") != fingerprint
        or manifest.get("inputs") != provenance
        or set(files) != expected
    ):
        raise ValueError("Отчет не соответствует входам и фиксации")
    for name, digest in files.items():
        if sha256((directory / name).read_bytes()) != digest:
            raise ValueError(f"Не совпадает контрольная сумма {split}/{name}")
    report = read_json(directory / "report.json")
    if (
        report.get("split") != split
        or report.get("threshold") != config["threshold"]
        or report.get("model") != config["model"]
    ):
        raise ValueError("Порог или модель в отчете отличаются от фиксации")
    if split == "holdout" and read_json(root / "holdout-receipt.json") != {
        "configuration_sha256": fingerprint,
        "inputs": provenance,
    }:
        raise ValueError("Квитанция holdout отличается от фиксации")


def evaluate(dataset, output, split, provider, describe):
    if split not in {"holdout", "retrospective"}:
        raise ValueError("Неизвестный режим оценки")
    root, reader = Path(output), DatasetReader(dataset)
    config, fingerprint = _load_config(root, reader)
    _verify_artifacts(
        root, "validation", config, fingerprint, _input(reader, "validation")[2]
    )
    if (root / split).exists():
        raise FileExistsError("Оценка уже сохранена")
    if describe() != config["model"]:
        raise ValueError("Модель отличается от зафиксированной")
    if split == "retrospective":
        evaluation_reader, sources, proposals, provenance = _saved_span_input()
    else:
        receipt = root / "holdout-receipt.json"
        if receipt.exists():
            raise FileExistsError("Holdout уже зарезервирован в этом каталоге")
        evaluation_reader = reader
        sources, proposals, provenance = _input(reader, "holdout")
        with receipt.open("xb") as stream:
            stream.write(
                json_bytes({"configuration_sha256": fingerprint, "inputs": provenance})
            )
    scored = _score(sources, proposals, provider)
    ranked = _rank(scored[0], scored[3], config["threshold"])
    if (
        describe() != config["model"]
        or _load_config(root, DatasetReader(dataset))[1] != fingerprint
    ):
        raise ValueError("Модель или фиксация изменилась во время оценки")
    _, labels = evaluation_reader.read_split("holdout")
    report, files = _report(split, scored, ranked, labels, config)
    files["manifest.json"] = _manifest(split, files, fingerprint, provenance)
    write_directory(root / split, files)
    return report


def verify(dataset, output):
    root, reader = Path(output), DatasetReader(dataset)
    config, fingerprint = _load_config(root, reader)
    results = {}
    for split in ("validation", "holdout"):
        _verify_artifacts(root, split, config, fingerprint, _input(reader, split)[2])
        reader.read_split(split)
        results[split] = "verified"
    if (root / "retrospective").exists():
        old_reader, _, _, provenance = _saved_span_input()
        _verify_artifacts(root, "retrospective", config, fingerprint, provenance)
        old_reader.read_split("holdout")
        results["retrospective"] = "verified"
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("calibrate", "holdout", "retrospective", "verify")
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = verify(args.dataset, args.output)
        else:
            describe = OllamaContextProvider(args.model).describe
            provider, _ = local_ollama(args.model)
            if args.command == "calibrate":
                result = calibrate(args.dataset, args.output, provider, describe)
            else:
                result = evaluate(
                    args.dataset, args.output, args.command, provider, describe
                )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json_bytes(result).decode(), end="")


if __name__ == "__main__":
    main()
