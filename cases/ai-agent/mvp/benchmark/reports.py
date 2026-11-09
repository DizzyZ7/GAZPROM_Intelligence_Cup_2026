"""Раздельные показатели качества, фактические ошибки и таблицы сравнения."""

from collections import defaultdict

from pulse import redact

from .artifact_io import csv_bytes, json_bytes
from .metrics import CATEGORIES, classification_metrics, pair_metrics, retrieval_metrics


def score_analysis(analysis, sources, labels, k=5):
    gold = {row["ticket_id"]: row for row in labels}
    originals = {row["id"]: row for row in sources}
    predictions = analysis["predictions"]
    truth = [
        gold[p["left"]]["gold_cluster_id"] is not None
        and gold[p["left"]]["gold_cluster_id"] == gold[p["right"]]["gold_cluster_id"]
        for p in analysis["pairs"]
    ]
    ranked = {row["ticket_id"]: [] for row in predictions}
    neighbors = defaultdict(list)
    for pair in analysis["pairs"]:
        if pair["matched"]:
            neighbors[pair["left"]].append((pair["score"], pair["right"]))
            neighbors[pair["right"]].append((pair["score"], pair["left"]))
    for identity in ranked:
        ranked[identity] = [
            n
            for _, n in sorted(
                neighbors[identity], key=lambda item: (-item[0], item[1])
            )[:k]
        ]
    quality = {
        "status": "ok",
        "method": analysis["method"],
        "threshold": analysis["threshold"],
        "classifier": analysis.get(
            "classifier", "rules-v1-shared; score не является вероятностью"
        ),
        "classification": classification_metrics(
            [gold[r["ticket_id"]]["category"] for r in predictions],
            [r["category"] for r in predictions],
        ),
        "retrieval_pairs": pair_metrics(
            truth, [p["matched"] for p in analysis["pairs"]]
        ),
        "retrieval_top_k": retrieval_metrics(
            {identity: row["gold_cluster_id"] for identity, row in gold.items()},
            ranked,
            k,
        ),
        "clustering": dict(
            pair_metrics(truth, [p["clustered"] for p in analysis["pairs"]]),
            signal_candidates=sum(len(g) > 1 for g in analysis["groups"]),
            singleton_count=sum(len(g) == 1 for g in analysis["groups"]),
        ),
    }
    errors = {
        "classification": [],
        "retrieval": {"false_matches": [], "missed_repeats": []},
        "clustering": {"false_matches": [], "missed_repeats": []},
    }
    for row in predictions:
        identity = row["ticket_id"]
        if row["category"] != gold[identity]["category"]:
            errors["classification"].append(
                {
                    "ticket_id": identity,
                    "expected": gold[identity]["category"],
                    "predicted": row["category"],
                    "text": redact(originals[identity]["text"])[:240],
                }
            )
    errors["classification"] = errors["classification"][:20]
    for task, field in (("retrieval", "matched"), ("clustering", "clustered")):
        for pair, expected in zip(analysis["pairs"], truth):
            if pair[field] == expected:
                continue
            reason = (
                "complete_link_split"
                if task == "clustering" and pair["matched"] and not pair["clustered"]
                else pair["reason"]
            )
            case = {
                "left": pair["left"],
                "right": pair["right"],
                "score": pair["score"],
                "reason": reason,
                "left_text": redact(originals[pair["left"]]["text"])[:240],
                "right_text": redact(originals[pair["right"]]["text"])[:240],
                "left_gold_issue": gold[pair["left"]]["issue_id"],
                "right_gold_issue": gold[pair["right"]]["issue_id"],
            }
            errors[task]["missed_repeats" if expected else "false_matches"].append(case)
        for kind, cases in errors[task].items():
            errors[task][kind] = sorted(
                cases,
                key=lambda p: (
                    -(p["score"] if p["score"] is not None else -2),
                    p["left"],
                    p["right"],
                ),
            )[:20]
    return quality, errors, ranked


def method_files(method, analysis, sources, labels, k=5, missing=None):
    prefix = method + "/"
    if analysis is None:
        report = dict(
            missing,
            method=method,
            classification=None,
            retrieval_pairs=None,
            retrieval_top_k=None,
            clustering=None,
        )
        return {
            prefix + "report.json": json_bytes(report),
            prefix + "performance.json": json_bytes(None),
        }, report
    report, errors, ranked = score_analysis(analysis, sources, labels, k)
    report["calibration"] = {
        key: value
        for key, value in (missing or {}).items()
        if key
        in (
            "calibration_status",
            "best_validation_f1",
            "best_validation_tp",
            "gold_positive_pairs",
        )
    }
    if (missing or {}).get("descriptor"):
        report["model"] = missing["descriptor"]
    gold = {row["ticket_id"]: row for row in labels}
    predictions = [
        dict(
            row,
            gold_category=gold[row["ticket_id"]]["category"],
            gold_workflow=gold[row["ticket_id"]]["workflow"],
            gold_cluster_id=gold[row["ticket_id"]]["gold_cluster_id"],
        )
        for row in analysis["predictions"]
    ]
    confusion = [
        {"gold_category": g, **report["classification"]["confusion_matrix"][g]}
        for g in CATEGORIES
    ]
    rows = [
        {"ticket_id": identity, "rank": rank + 1, "neighbor_id": neighbor}
        for identity, neighbors in ranked.items()
        for rank, neighbor in enumerate(neighbors)
    ]
    files = {
        prefix + "report.json": json_bytes(report),
        prefix + "performance.json": json_bytes(analysis["performance"]),
        prefix + "errors.json": json_bytes(errors),
        prefix + "groups.json": json_bytes(analysis["groups"]),
        prefix + "predictions.csv": csv_bytes(
            tuple(predictions[0])
            if predictions
            else (
                "ticket_id",
                "product",
                "category",
                "workflow",
                "classifier_score",
                "cluster_id",
                "gold_category",
                "gold_workflow",
                "gold_cluster_id",
            ),
            predictions,
        ),
        prefix + "confusion.csv": csv_bytes(("gold_category", *CATEGORIES), confusion),
        prefix + "retrieval.csv": csv_bytes(("ticket_id", "rank", "neighbor_id"), rows),
    }
    return files, report


def comparison_files(reports, performance, *, split="holdout"):
    warnings = []
    rows, lines = (
        [],
        [
            f"# Сравнение методов на {split}",
            "",
            "| Метод | Статус | Категория: macro F1 | Поиск: F1 | Кластеры: pairwise F1 | Ложные объединения |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ],
    )
    for method, report in reports.items():
        good = report["status"] == "ok"
        row = {
            "method": method,
            "status": report["status"],
            "classification_macro_f1": None,
            "retrieval_precision": None,
            "retrieval_recall": None,
            "retrieval_f1": None,
            "cluster_precision": None,
            "cluster_recall": None,
            "cluster_f1": None,
            "false_merge_fraction": None,
            "missed_repeat_fraction": None,
            "total_seconds": None,
            "tickets_per_second": None,
            "python_peak_bytes": None,
        }
        if good:
            row["classification_macro_f1"] = report["classification"]["macro_f1"]
            for task, prefix in (
                ("retrieval_pairs", "retrieval"),
                ("clustering", "cluster"),
            ):
                for metric in ("precision", "recall", "f1"):
                    row[prefix + "_" + metric] = report[task][metric]
            for metric in ("false_merge_fraction", "missed_repeat_fraction"):
                row[metric] = report["clustering"][metric]
            for metric in ("total_seconds", "tickets_per_second", "python_peak_bytes"):
                row[metric] = performance[method][metric]
        rows.append(row)
        calibration = report.get("calibration", {})
        if calibration.get("calibration_status") in {
            "no_true_positives",
            "no_positive_gold",
        }:
            warnings.append(
                f"{method}: validation не подтверждает полезный порог (TP={calibration['best_validation_tp']}, эталонных повторных пар={calibration['gold_positive_pairs']}). Порог выбран формальным правилом равенства, его качество не подтверждено."
            )
        value = lambda x: f"{x:.4f}" if isinstance(x, (int, float)) else "—"
        status = (
            "выполнено"
            if good
            else ("не проверено" if report["status"] == "not_checked" else "ошибка")
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    method,
                    status,
                    value(row["classification_macro_f1"]),
                    value(row["retrieval_f1"]),
                    value(row["cluster_f1"]),
                    value(row["false_merge_fraction"]),
                )
            )
            + " |"
        )
    lines += [
        "",
        *warnings,
        "",
        "Rules, TF-IDF и Embeddings используют общий выбранный классификатор. При контекстном анализе legacy_rules отдельно показывает прежний baseline. Метки ограничений вычислены из текста; gold не используется для ограничения кандидатов.",
        "Порог выбран на validation до финальной оценки. Отчеты задач и примеры ошибок сохранены отдельно.",
        "Набор синтетический, экспертная проверка ожидается. Результаты не подтверждают качество на корпоративном потоке.",
        "Время и Python peak memory относятся к одному локальному запуску с tracemalloc; это не нагрузочный тест API и не измерение памяти Ollama/GPU.",
        "",
    ]
    return {
        "comparison.csv": csv_bytes(tuple(rows[0]), rows),
        "comparison.md": "\n".join(lines).encode(),
    }
