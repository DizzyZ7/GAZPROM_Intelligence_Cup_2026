"""Парная офлайн-оценка цитат и серверной нормализации на одних предсказаниях."""

import argparse
import time
from collections import Counter
from importlib.metadata import version
from itertools import combinations
from pathlib import Path

from benchmark.artifact_io import json_bytes, read_json, sha256, write_directory
from benchmark.engine import _attach_groups, _context, analyze_method
from benchmark.protocol import DatasetReader, source_fingerprints
from benchmark.reports import comparison_files, method_files, score_analysis
from pulse import Ticket
from span_context import (
    PROMPT,
    SPAN_OPTIONS,
    VERSION,
    OllamaSpanProvider,
    extract_spans,
    normalize_fragment,
    safe_text,
)
from structured_context import MODEL
from structured_eval import SLOT_MIN_F1, _check_gold, _select, _slot_hit
from ticket_context import ACTIONABLE

_SPLITS = ("validation", "holdout")
_METHODS = ("rules_v2_3", "literal", "normalized")
_METHOD_FILES = (
    "report.json",
    "performance.json",
    "errors.json",
    "groups.json",
    "predictions.csv",
    "confusion.csv",
    "retrieval.csv",
)
_ARTIFACTS = {"comparison.csv", "diagnostic.json", "evidence.json"} | {
    f"{method}/{name}" for method in _METHODS for name in _METHOD_FILES
}
_INFERENCE_FAILURES = (
    "provider_unavailable",
    "invalid_response",
    "invalid_schema",
    "invalid_proposal",
)


def _configuration(reference, holdout):
    root = Path(__file__).parent
    sources = {
        name: sha256((root / name).read_bytes())
        for name in (
            "span_context.py",
            "span_eval.py",
            "structured_context.py",
            "structured_eval.py",
        )
    }
    return {
        "version": VERSION,
        "reference_sha256": reference.fingerprint,
        "holdout_sha256": holdout.fingerprint,
        "sources": source_fingerprints() | sources,
        "snowballstemmer_version": version("snowballstemmer"),
        "prompt_sha256": sha256(PROMPT.encode()),
        "options": SPAN_OPTIONS,
        "slot_min_f1": SLOT_MIN_F1,
        "pairing": "exact_product_category_operation_symptom",
        "gold_quote_diagnostic": "first_annotated_alternative",
    }


def freeze(reference_dir, holdout_dir, output, provider):
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    if (
        reference.fingerprint == holdout.fingerprint
        or not 0 < holdout.manifest["counts"]["holdout"] <= 64
    ):
        raise ValueError("Требуется отдельный контрольный набор до 64 обращений")
    config = dict(_configuration(reference, holdout), model=provider.describe())
    write_directory(output, {"frozen-config.json": json_bytes(config)})


def _load_config(root, reference, holdout):
    path = root / "frozen-config.json"
    data = path.read_bytes()
    config = read_json(path)
    if {
        key: value for key, value in config.items() if key != "model"
    } != _configuration(reference, holdout):
        raise ValueError("Код, параметры или наборы отличаются от фиксации")
    return config, sha256(data)


def _collect(sources, safe_sources, provider):
    evidence = []
    for row, safe in zip(sources, safe_sources):
        item = extract_spans(row["text"], provider.propose)
        evidence.append(
            {
                "ticket_id": row["id"],
                "sanitized_text_sha256": sha256(safe["text"].encode()),
                "category": item.context.category,
                "reason": item.context.reason,
                "model_called": item.model_called,
                "admitted": item.context.groupable,
                "operation_span": item.operation_span,
                "symptom_span": item.symptom_span,
            }
        )
    return evidence


def _analysis(sources, evidence, method, inference_seconds=0):
    started = time.perf_counter()
    predictions = []
    for source, item in zip(sources, evidence):
        keys = [None, None]
        if item["admitted"]:
            for i, slot in enumerate(("operation", "symptom")):
                start, end = item[f"{slot}_span"]
                fragment = source["text"][start:end]
                keys[i] = (
                    normalize_fragment(fragment) if method == "normalized" else fragment
                )
        predictions.append(
            {
                "ticket_id": source["id"],
                "product": source["product"],
                "category": item["category"],
                "workflow": "quoted_operation" if item["admitted"] else "unclassified",
                "operation": keys[0],
                "symptom": keys[1],
                "groupable": item["admitted"],
                "reason": item["reason"],
            }
        )
    pairs = []
    for left, right in combinations(predictions, 2):
        reason = _context(left, right)
        matched = reason == "context_pass"
        pairs.append(
            {
                "left": left["ticket_id"],
                "right": right["ticket_id"],
                "score": 1.0 if matched else None,
                "matched": matched,
                "reason": f"{method}_key_match" if matched else reason,
            }
        )
    groups = _attach_groups(predictions, pairs)
    pairing_seconds = time.perf_counter() - started
    total = inference_seconds + pairing_seconds
    return {
        "method": method,
        "classifier": VERSION,
        "threshold": None,
        "predictions": predictions,
        "pairs": pairs,
        "groups": groups,
        "performance": {
            "total_seconds": total,
            "classification_seconds": inference_seconds,
            "pairing_seconds": pairing_seconds,
            "tickets_per_second": len(sources) / total,
            "python_peak_bytes": None,
        },
    }


def _diagnostic(sources, labels, evidence):
    gold = {row["ticket_id"]: row for row in labels}
    counts = dict(
        labeled=0,
        operation_hit=0,
        symptom_hit=0,
        both_hit=0,
        admitted_both_hit=0,
        must_abstain=0,
    )
    violations, oracle = [], []
    for source, item in zip(sources, evidence):
        label = gold[source["id"]]
        annotation = label.get("ground_truth", {})
        mandatory = annotation.get("must_abstain") is True
        if mandatory:
            counts["must_abstain"] += 1
            if item["admitted"]:
                violations.append(source["id"])
        spans = []
        if (
            label["category"] in ACTIONABLE
            and not mandatory
            and all(
                annotation.get(f"{slot}_evidence") for slot in ("operation", "symptom")
            )
        ):
            counts["labeled"] += 1
            hits = []
            for slot in ("operation", "symptom"):
                alternatives = [
                    safe_text(quote) for quote in annotation[f"{slot}_evidence"]
                ]
                hit = _slot_hit(source["text"], item[f"{slot}_span"], alternatives)
                hits.append(hit)
                counts[f"{slot}_hit"] += hit
                start = source["text"].find(alternatives[0])
                if not alternatives[0] or start < 0:
                    raise ValueError(
                        "Эталонная цитата отсутствует в обезличенном тексте"
                    )
                spans.append((start, start + len(alternatives[0])))
            counts["both_hit"] += all(hits)
            counts["admitted_both_hit"] += all(hits) and item["admitted"]
        oracle.append(
            {
                "category": label["category"],
                "admitted": bool(spans),
                "reason": "gold_quote_diagnostic",
                "operation_span": spans[0] if spans else None,
                "symptom_span": spans[1] if spans else None,
            }
        )
    return {
        "fragment_quality": counts | {"accepted_despite_abstention": violations},
        "gold_quotes_only": {
            method: score_analysis(_analysis(sources, oracle, method), sources, labels)[
                0
            ]["retrieval_pairs"]
            for method in ("literal", "normalized")
        }
        if counts["labeled"]
        else None,
    }


def _verify_split(root, split, config_sha, reader):
    directory = root / split
    manifest = read_json(directory / "manifest.json")
    files = manifest.get("files", {})
    inputs, _ = reader.read_split(split, gold=False)
    sources = _select(inputs, split)
    if (
        manifest.get("split") != split
        or manifest.get("status") not in {"complete", "partial", "not_checked"}
        or manifest.get("configuration_sha256") != config_sha
        or manifest.get("dataset_manifest_sha256") != reader.fingerprint
        or manifest.get("inputs_sha256") != sha256(json_bytes(sources))
        or set(files) != _ARTIFACTS
    ):
        raise ValueError("Отчет не соответствует зафиксированным входам")
    if split == "holdout" and read_json(root / "holdout-receipt.json") != {
        "configuration_sha256": config_sha,
        "inputs_sha256": manifest["inputs_sha256"],
    }:
        raise ValueError("Квитанция holdout не соответствует входам")
    for name, digest in files.items():
        if sha256((directory / name).read_bytes()) != digest:
            raise ValueError(f"Не совпадает контрольная сумма {split}/{name}")
    evidence = read_json(directory / "evidence.json")
    if [item["ticket_id"] for item in evidence] != [row["id"] for row in sources]:
        raise ValueError("Доказательства не соответствуют выборке")
    for row, item in zip(sources, evidence):
        text = safe_text(row["text"])
        if sha256(text.encode()) != item["sanitized_text_sha256"]:
            raise ValueError("Изменился обезличенный текст доказательства")
        for field in ("operation_span", "symptom_span"):
            span = item[field]
            if span is None and not item["admitted"]:
                continue
            if (
                not isinstance(span, list)
                or len(span) != 2
                or any(type(x) is not int for x in span)
                or not 0 <= span[0] < span[1] <= len(text)
            ):
                raise ValueError("Неверные границы фрагмента")
    if any(
        read_json(directory / method / "report.json").get("inference_status")
        != manifest["status"]
        for method in ("literal", "normalized")
    ):
        raise ValueError("Статус модели отличается от манифеста")
    return manifest["status"]


def evaluate(reference_dir, holdout_dir, output, split, provider):
    if split not in _SPLITS:
        raise ValueError("Неизвестная выборка")
    root = Path(output)
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    config, config_sha = _load_config(root, reference, holdout)
    if (root / split).exists():
        raise FileExistsError("Выборка уже оценена по этой фиксации")
    if split == "holdout" and (
        not (root / "validation/manifest.json").is_file()
        or _verify_split(root, "validation", config_sha, reference) != "complete"
    ):
        raise ValueError("Перед holdout требуется завершенная оценка validation")
    if provider.describe() != config["model"]:
        raise ValueError("Модель отличается от зафиксированной")

    reader = holdout if split == "holdout" else reference
    inputs, _ = reader.read_split(split, gold=False)
    sources = _select(inputs, split)
    inputs_sha = sha256(json_bytes(sources))
    if split == "holdout":
        reservation = {"configuration_sha256": config_sha, "inputs_sha256": inputs_sha}
        with (root / "holdout-receipt.json").open("xb") as stream:
            stream.write(json_bytes(reservation))
    safe_sources = [dict(row, text=safe_text(row["text"])) for row in sources]
    baseline = analyze_method(
        "rules", [Ticket(**row) for row in sources], classifier="context"
    )
    started = time.perf_counter()
    evidence = _collect(sources, safe_sources, provider)
    inference_seconds = time.perf_counter() - started
    analyses = {"rules_v2_3": baseline} | {
        method: _analysis(safe_sources, evidence, method, inference_seconds)
        for method in ("literal", "normalized")
    }
    if provider.describe() != config["model"]:
        raise ValueError("Модель изменилась во время оценки")
    if _load_config(root, reference, holdout)[1] != config_sha:
        raise ValueError("Фиксация изменилась во время оценки")

    # Эталоны недоступны до завершения извлечения и обеих группировок.
    _, all_labels = reader.read_split(split)
    selected = {row["id"] for row in sources}
    labels = [row for row in all_labels if row["ticket_id"] in selected]
    _check_gold(labels)
    reasons = Counter(item["reason"] for item in evidence)
    calls = sum(item["model_called"] for item in evidence)
    failures = sum(reasons[reason] for reason in _INFERENCE_FAILURES)
    status = (
        "not_checked" if calls == failures else "partial" if failures else "complete"
    )
    diagnostic = _diagnostic(safe_sources, labels, evidence) | {
        "model": config["model"],
        "model_calls": calls,
        "inference_seconds": inference_seconds,
        "spans_both_verified": sum(
            item["operation_span"] is not None and item["symptom_span"] is not None
            for item in evidence
        ),
        "admitted": sum(item["admitted"] for item in evidence),
        "decision_reasons": dict(reasons),
    }
    files, reports, performances = {}, {}, {}
    for method, analysis in analyses.items():
        artifacts, report = method_files(method, analysis, safe_sources, labels)
        if method != "rules_v2_3":
            report.update(
                model=config["model"],
                inference_status=status,
                status="ok" if status == "complete" else status,
            )
            artifacts[f"{method}/report.json"] = json_bytes(report)
        files.update(artifacts)
        reports[method], performances[method] = report, analysis["performance"]
    files.update(
        {
            "comparison.csv": comparison_files(reports, performances, split=split)[
                "comparison.csv"
            ],
            "diagnostic.json": json_bytes(diagnostic),
            "evidence.json": json_bytes(evidence),
        }
    )
    manifest = {
        "split": split,
        "status": status,
        "configuration_sha256": config_sha,
        "dataset_manifest_sha256": reader.fingerprint,
        "inputs_sha256": inputs_sha,
        "files": {name: sha256(data) for name, data in files.items()},
    }
    write_directory(root / split, files | {"manifest.json": json_bytes(manifest)})
    return manifest


def verify(reference_dir, holdout_dir, output):
    root = Path(output)
    reference, holdout = DatasetReader(reference_dir), DatasetReader(holdout_dir)
    _, config_sha = _load_config(root, reference, holdout)
    results = {}
    for split, reader in zip(_SPLITS, (reference, holdout)):
        results[split] = _verify_split(root, split, config_sha, reader)
        reader.read_split(split)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "evaluate", "verify"))
    parser.add_argument("--reference", required=True)
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--split", choices=_SPLITS)
    args = parser.parse_args()
    common = args.reference, args.holdout, args.output
    try:
        if args.command == "verify":
            result = verify(*common)
        elif args.command == "freeze":
            freeze(*common, OllamaSpanProvider(args.model))
            result = read_json(Path(args.output) / "frozen-config.json")["model"]
        else:
            if args.split is None:
                parser.error("Для evaluate требуется --split")
            manifest = evaluate(*common, args.split, OllamaSpanProvider(args.model))
            result = {
                key: manifest[key]
                for key in ("split", "status", "configuration_sha256")
            }
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json_bytes(result).decode(), end="")


if __name__ == "__main__":
    main()
