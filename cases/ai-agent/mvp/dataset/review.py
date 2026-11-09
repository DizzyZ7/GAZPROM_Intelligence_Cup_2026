"""Слепой пакет разметки и проверка полученных экспертных аннотаций."""

import argparse
import csv
import io
import json
import random
from itertools import combinations
from pathlib import Path

from benchmark.artifact_io import (
    csv_bytes,
    json_bytes,
    read_json,
    sha256,
    write_directory,
)
from benchmark.metrics import CATEGORIES, classification_metrics, pair_metrics
from benchmark.protocol import DatasetReader
from pulse import redact

FEATURES = ("ticket_id", "product_id", "text", "source_channel")
ANNOTATIONS = (
    "reviewer_category",
    "reviewer_workflow",
    "reviewer_cluster_id",
    "reviewer_comment",
)
FIELDS = FEATURES + ANNOTATIONS
VERSION = "pulse-blind-review-v1"


def _packet(reader, count, seed):
    if type(count) is not int or not 0 < count <= 500 or type(seed) is not int:
        raise ValueError("Допустимы от 1 до 500 примеров и целочисленный seed")
    sources = []
    for split in ("development", "validation"):
        rows, _ = reader.read_split(split, gold=False)
        sources.extend(rows)
    if len({row["id"] for row in sources}) != len(sources) or count > len(sources):
        raise ValueError("Недостаточно уникальных источников для проверки")
    selected = random.Random(seed).sample(
        sorted(sources, key=lambda row: row["id"]), count
    )
    rows = [
        dict(
            ticket_id=row["id"],
            product_id=row["product"],
            text=redact(row["text"]),
            source_channel=row["channel"],
            **dict.fromkeys(ANNOTATIONS, ""),
        )
        for row in selected
    ]
    packet = csv_bytes(FIELDS, rows)
    return packet, [row["ticket_id"] for row in rows]


def prepare_review(dataset_dir, output, *, count=40, seed=20261109):
    reader = DatasetReader(dataset_dir)
    packet, ids = _packet(reader, count, seed)
    manifest = {
        "version": VERSION,
        "status": "pending",
        "seed": seed,
        "count": count,
        "dataset_manifest_sha256": reader.fingerprint,
        "splits": ["development", "validation"],
        "ticket_ids": ids,
        "review_csv_sha256": sha256(packet),
    }
    write_directory(
        output, {"review.csv": packet, "manifest.json": json_bytes(manifest)}
    )
    return manifest


def _rows(content):
    if len(content) > 32 * 1024 * 1024:
        raise ValueError("Слишком большой файл разметки")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Колонки разметки отличаются от слепого пакета")
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("Неверный CSV разметки") from exc
    if any(
        set(row) != set(FIELDS) or any(value is None for value in row.values())
        for row in rows
    ):
        raise ValueError("Неполная строка разметки")
    return rows


def assess_review(dataset_dir, packet_dir, completed_csv, *, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
        raise ValueError("Укажите рецензента")
    reader = DatasetReader(dataset_dir)
    root = Path(packet_dir)
    manifest = read_json(root / "manifest.json")
    packet = (root / "review.csv").read_bytes()
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "version",
            "status",
            "seed",
            "count",
            "dataset_manifest_sha256",
            "splits",
            "ticket_ids",
            "review_csv_sha256",
        }
        or manifest.get("version") != VERSION
        or manifest.get("status") != "pending"
        or manifest.get("splits") != ["development", "validation"]
        or manifest.get("dataset_manifest_sha256") != reader.fingerprint
        or manifest.get("review_csv_sha256") != sha256(packet)
    ):
        raise ValueError("Пакет или версия набора изменены")
    canonical, expected_ids = _packet(reader, manifest["count"], manifest["seed"])
    if packet != canonical or manifest["ticket_ids"] != expected_ids:
        raise ValueError("Пакет не соответствует исходным доказательствам и выборке")
    original = {row["ticket_id"]: row for row in _rows(packet)}
    completed = Path(completed_csv).read_bytes()
    rows = _rows(completed)
    ids = [row["ticket_id"] for row in rows]
    if (
        len(ids) != len(set(ids))
        or set(ids) != set(original)
        or set(ids) != set(manifest["ticket_ids"])
    ):
        raise ValueError("Состав источников разметки отличается от пакета")
    for row in rows:
        if any(row[field] != original[row["ticket_id"]][field] for field in FEATURES):
            raise ValueError("Исходные доказательства изменены")
        if (
            row["reviewer_category"] not in CATEGORIES
            or not row["reviewer_workflow"].strip()
            or len(row["reviewer_workflow"]) > 128
            or len(row["reviewer_cluster_id"]) > 128
            or len(row["reviewer_comment"]) > 1000
        ):
            raise ValueError("Заполните категорию и сценарий каждого примера")
        if (
            row["reviewer_category"] not in {"bug", "feature_request"}
            and row["reviewer_cluster_id"].strip()
        ):
            raise ValueError("Непродуктовое обращение не может входить в кластер")
    # Эталоны открываются только после проверки полноты и неизменности слепого пакета.
    gold = {}
    for split in ("development", "validation"):
        _, labels = reader.read_split(split)
        gold.update({row["ticket_id"]: row for row in labels})
    truth, predicted = [], []
    for left, right in combinations(rows, 2):
        l_gold, r_gold = gold[left["ticket_id"]], gold[right["ticket_id"]]
        truth.append(
            l_gold["gold_cluster_id"] is not None
            and l_gold["gold_cluster_id"] == r_gold["gold_cluster_id"]
        )
        predicted.append(
            bool(left["reviewer_cluster_id"].strip())
            and left["product_id"] == right["product_id"]
            and left["reviewer_category"] == right["reviewer_category"]
            and left["reviewer_cluster_id"].strip()
            == right["reviewer_cluster_id"].strip()
        )
    return {
        "version": VERSION,
        "status": "submitted",
        "reviewer": redact(reviewer.strip()),
        "reviewed_tickets": len(rows),
        "dataset_manifest_sha256": reader.fingerprint,
        "completed_csv_sha256": sha256(completed),
        "classification": classification_metrics(
            [gold[row["ticket_id"]]["category"] for row in rows],
            [row["reviewer_category"] for row in rows],
        ),
        "pairwise_annotation_agreement": pair_metrics(truth, predicted),
        "workflow_agreement": sum(
            row["reviewer_workflow"] == gold[row["ticket_id"]]["workflow"]
            for row in rows
        )
        / len(rows),
        "disagreements": [
            {
                "ticket_id": row["ticket_id"],
                "gold_category": gold[row["ticket_id"]]["category"],
                "reviewer_category": row["reviewer_category"],
                "gold_workflow": gold[row["ticket_id"]]["workflow"],
                "reviewer_workflow": row["reviewer_workflow"],
                "comment": redact(row["reviewer_comment"]),
            }
            for row in rows
            if row["reviewer_category"] != gold[row["ticket_id"]]["category"]
            or row["reviewer_workflow"] != gold[row["ticket_id"]]["workflow"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    check = sub.add_parser("check")
    for command in (prepare, check):
        command.add_argument("--dataset", required=True)
        command.add_argument("--output", required=True)
    prepare.add_argument("--count", type=int, default=40)
    prepare.add_argument("--seed", type=int, default=20261109)
    check.add_argument("--packet", required=True)
    check.add_argument("--completed", required=True)
    check.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            report = prepare_review(
                args.dataset, args.output, count=args.count, seed=args.seed
            )
        else:
            report = assess_review(
                args.dataset, args.packet, args.completed, reviewer=args.reviewer
            )
            with Path(args.output).open("xb") as stream:
                stream.write(json_bytes(report))
        print(json.dumps(report, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(
            2,
            f"Ошибка проверки разметки: {type(exc).__name__}; проверьте пакет и заполнение CSV.\n",
        )


if __name__ == "__main__":
    main()
