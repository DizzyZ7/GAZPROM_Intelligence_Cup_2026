"""Сравнение фильтров и методов поиска на validation."""

import argparse
import json
from pathlib import Path

from benchmark.engine import analyze_method, apply_threshold
from benchmark.metrics import pair_metrics
from benchmark.protocol import GRID, DatasetReader, local_ollama
from benchmark.tfidf import TfidfModel
from pulse import Ticket, redact


def _quality(analysis, truth):
    return {
        "retrieval": pair_metrics(truth, [p["matched"] for p in analysis["pairs"]]),
        "clustering": pair_metrics(truth, [p["clustered"] for p in analysis["pairs"]]),
    }


def _truth(analysis, labels):
    gold = {row["ticket_id"]: row["gold_cluster_id"] for row in labels}
    return [
        gold[p["left"]] is not None and gold[p["left"]] == gold[p["right"]]
        for p in analysis["pairs"]
    ]


def compare(dataset, fp_budget=10, provider=None, check_holdout=False):
    reader = DatasetReader(dataset)
    development, _ = reader.read_split("development", gold=False)
    sources, labels = reader.read_split("validation")
    model = TfidfModel.fit([redact(row["text"]) for row in development])
    tickets = [Ticket(**row) for row in sources]
    words = TfidfModel(
        {t: w for t, w in model.idf.items() if t.startswith("w")}, model.document_count
    )
    chars = TfidfModel(
        {t: w for t, w in model.idf.items() if t.startswith("c")}, model.document_count
    )
    configs = {
        "strict": {"tfidf": model},
        "include_unknown": {"tfidf": model, "include_unknown": True},
        "word_tfidf": {"tfidf": words, "include_unknown": True},
        "char_tfidf": {"tfidf": chars, "include_unknown": True},
    }
    if provider is not None:
        configs["embeddings"] = {"provider": provider, "include_unknown": True}

    def analyze(name, tickets, threshold):
        method = "embeddings" if name == "embeddings" else "tfidf"
        return analyze_method(method, tickets, threshold, **configs[name])

    results = {}
    for name in configs:
        analysis = analyze(name, tickets, GRID[0])
        pairs = analysis["pairs"]
        truth = _truth(analysis, labels)
        thresholds = [
            {
                "threshold": threshold,
                **_quality(apply_threshold(analysis, threshold), truth),
            }
            for threshold in GRID
        ]
        results[name] = {
            "candidate_filter": pair_metrics(truth, [p["score"] is not None for p in pairs]),
            "within_fp_budget": max(
                (row for row in thresholds if row["retrieval"]["fp"] <= fp_budget),
                key=lambda row: (
                    row["retrieval"]["tp"],
                    -row["retrieval"]["fp"],
                    row["threshold"],
                ),
                default=None,
            ),
            "threshold_grid": thresholds,
        }
    report = {
        "split": "validation",
        "dataset_fingerprint": reader.fingerprint,
        "tickets": len(tickets),
        "fp_budget": fp_budget,
        "results": results,
    }
    if check_holdout:
        baseline = results["include_unknown"]["within_fp_budget"]
        baseline_tp = baseline["retrieval"]["tp"] if baseline else 0
        winners = {
            name: row["within_fp_budget"]
            for name, row in results.items()
            if name not in ("strict", "include_unknown")
            and row["within_fp_budget"] is not None
            and row["within_fp_budget"]["retrieval"]["tp"] > baseline_tp
        }
        if not winners or baseline is None:
            report["holdout"] = {"status": "no_validation_winner"}
            return report
        winner = max(
            winners,
            key=lambda name: (
                winners[name]["retrieval"]["tp"],
                -winners[name]["retrieval"]["fp"],
            ),
        )
        holdout, gold = reader.read_split("holdout")
        tickets = [Ticket(**row) for row in holdout]
        report["holdout"] = {"status": "evaluated", "winner_on_validation": winner}
        for name, selected in (("include_unknown", baseline), (winner, winners[winner])):
            threshold = selected["threshold"]
            analysis = analyze(name, tickets, threshold)
            report["holdout"][name] = {
                "threshold": threshold,
                **_quality(analysis, _truth(analysis, gold)),
            }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).with_name("datasets") / "pulse-synthetic-v1",
        help="Каталог датасета с исходными окончаниями строк LF",
    )
    parser.add_argument("--fp-budget", type=int, default=10)
    parser.add_argument("--ollama-model", help="Имя установленной embedding-модели")
    parser.add_argument("--check-holdout", action="store_true")
    args = parser.parse_args()
    if args.fp_budget < 0:
        parser.error("--fp-budget должен быть неотрицательным")
    provider, descriptor = local_ollama(args.ollama_model) if args.ollama_model else (None, None)
    report = compare(args.dataset, args.fp_budget, provider, args.check_holdout)
    if descriptor:
        report["embedding_model"] = descriptor
    print(json.dumps(report, ensure_ascii=False, indent=2))
