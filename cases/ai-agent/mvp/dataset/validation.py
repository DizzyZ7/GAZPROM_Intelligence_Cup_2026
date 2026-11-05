"""Структурные проверки набора, границ выборок и эталонного потока событий."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from .generator import DATASET_VERSION, SPLITS

INPUT_FIELDS = {"id", "product", "text", "channel"}
EVENT_FIELDS = {
    "event_id",
    "ticket_id",
    "product_id",
    "text",
    "source_channel",
    "revision",
    "event_type",
    "batch_id",
    "occurred_at",
}
CATEGORIES = {"bug", "feature_request", "how_to", "service_request", "other", "unknown"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _validate_events(data, sources):
    events, labels = data["events"], data["event_labels"]
    _require(len(events) == len(labels), "Число событий и эталонов различается")
    seen, revisions, created = {}, {}, set()
    for event, label in zip(events, labels):
        _require(set(event) == EVENT_FIELDS, "Неверная схема события")
        _require(
            event["event_id"] == label["event_id"], "Эталон относится к другому событию"
        )
        ticket_id = event["ticket_id"]
        _require(ticket_id in sources, "Событие ссылается на отсутствующее обращение")
        source = sources[ticket_id]
        _require(event["product_id"] == source["product"], "Подмена продукта в событии")
        _require(
            event["source_channel"] == source["channel"], "Подмена канала в событии"
        )
        _require(
            type(event["revision"]) is int and event["revision"] >= 1,
            "Неверная версия события",
        )
        payload = {k: v for k, v in event.items() if k != "batch_id"}
        if event["event_id"] in seen:
            _require(
                seen[event["event_id"]] == payload,
                "ID события повторен с другим содержимым",
            )
            expected = "ignore_duplicate"
        elif ticket_id not in revisions:
            _require(
                event["revision"] == 1 and event["event_type"] == "created",
                "Первое событие должно создавать версию 1",
            )
            _require(event["text"] == source["text"], "Исходный текст события изменен")
            created.add(ticket_id)
            expected = "insert"
        else:
            _require(
                event["revision"] == revisions[ticket_id] + 1
                and event["event_type"] == "updated",
                "Нарушен порядок версий",
            )
            _require(event["text"] != source["text"], "Обновление не меняет текст")
            expected = "update"
        _require(
            label["expected_action"] == expected, "Неверный эталон обработки события"
        )
        seen[event["event_id"]] = payload
        revisions[ticket_id] = max(revisions.get(ticket_id, 0), event["revision"])
    _require(created == set(sources), "Не у всех обращений есть исходное событие")


def validate_dataset(bundle):
    """Проверить инварианты. Семантическую верность разметки проверяют эксперты."""
    _require(
        bundle["dataset_version"] == DATASET_VERSION and bundle["schema_version"] == 1,
        "Неподдерживаемая версия набора",
    )
    _require(set(bundle["splits"]) == set(SPLITS), "Неверные имена выборок")
    global_ids, global_families, global_clusters, global_texts = (
        set(),
        set(),
        set(),
        set(),
    )
    total = 0
    counts = {}
    for split in SPLITS:
        data = bundle["splits"][split]
        _require(
            set(data) == {"inputs", "labels", "events", "event_labels"},
            "Неполная выборка",
        )
        inputs, labels = data["inputs"], data["labels"]
        _require(
            len(inputs) == len(labels) and bool(inputs),
            "Число признаков и эталонов различается",
        )
        sources = {}
        for source in inputs:
            _require(
                set(source) == INPUT_FIELDS,
                "В признаках запрещены дополнительные поля, включая gold",
            )
            _require(
                all(isinstance(v, str) and v.strip() for v in source.values()),
                "Пустой или неверный признак",
            )
            _require(
                source["id"] not in sources and source["id"] not in global_ids,
                "Повторный ID обращения",
            )
            _require(len(source["text"]) <= 10000, "Текст превышает контракт MVP")
            sources[source["id"]] = source
        _require(
            {row["ticket_id"] for row in labels} == set(sources),
            "Нет взаимно однозначного соответствия разметки",
        )
        clusters = Counter(
            row["gold_cluster_id"] for row in labels if row["gold_cluster_id"]
        )
        families, texts = (
            set(),
            {" ".join(row["text"].casefold().split()) for row in inputs},
        )
        for source, label in zip(inputs, labels):
            _require(
                label["ticket_id"] == source["id"],
                "Порядок эталонов и признаков различается",
            )
            _require(
                label["product_id"] == source["product"]
                and label["source_channel"] == source["channel"],
                "Разметка не соответствует продукту или каналу",
            )
            _require(label["category"] in CATEGORIES, "Неизвестная категория")
            _require(
                label["difficulty"] in {"easy", "medium", "hard"},
                "Неизвестная сложность",
            )
            _require(
                type(label["expected_signal"]) is bool, "Неверный тип expected_signal"
            )
            truth = label["ground_truth"]
            families.add(truth["split_group_id"])
            families.update(issue["issue_id"] for issue in truth["issues"])
            _require(
                bool(truth["issues"])
                and truth["issues"][0]["issue_id"] == label["issue_id"],
                "Неполная разметка проблем",
            )
            primary = truth["issues"][0]
            _require(
                (primary["category"], primary["workflow"], primary["normalized_intent"])
                == (label["category"], label["workflow"], label["normalized_intent"]),
                "Противоречивая разметка намерения",
            )
            _require(
                truth["split_group_id"] == label["issue_id"],
                "Группа не соответствует семейству проблемы",
            )
            multi = truth["requires_decomposition"]
            _require(
                type(multi) is bool and multi == (len(truth["issues"]) > 1),
                "Неверная разметка нескольких проблем",
            )
            if multi or label["category"] not in {"bug", "feature_request"}:
                _require(
                    label["gold_cluster_id"] is None,
                    "Нерелевантный или многопроблемный текст включен в кластер",
                )
            else:
                _require(
                    label["gold_cluster_id"]
                    == f"{source['product']}|{label['issue_id']}",
                    "Подмена gold-кластера",
                )
            expected = bool(
                label["gold_cluster_id"] and clusters[label["gold_cluster_id"]] >= 2
            )
            _require(
                label["expected_signal"] == expected,
                "Сигнал не подтвержден независимыми обращениями",
            )
            if "rare_singleton" in truth["case_types"]:
                _require(
                    not expected and clusters[label["gold_cluster_id"]] == 1,
                    "Редкая проблема имеет повтор",
                )
        _require(not (global_families & families), "Утечка семейства между выборками")
        _require(
            not (global_clusters & set(clusters)), "Утечка кластера между выборками"
        )
        _require(
            not (global_texts & texts), "Одинаковый текст находится в разных выборках"
        )
        _validate_events(data, sources)
        global_ids.update(sources)
        global_families.update(families)
        global_clusters.update(clusters)
        global_texts.update(texts)
        total += len(inputs)
        counts[split] = len(inputs)
    _require(total == bundle["size"], "Размер набора не соответствует конфигурации")
    expected_counts = (total * 3 // 5, total // 5, total - total * 3 // 5 - total // 5)
    _require(tuple(counts.values()) == expected_counts, "Нарушены пропорции 60/20/20")
    return {
        "status": "valid",
        "size": total,
        "counts": counts,
        "expert_review": "pending",
    }


def load_dataset(directory):
    """Прочитать только известные файлы, проверить SHA-256, затем структуру."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        expected_files = {"expert-review.csv"} | {
            f"{split}.{kind}.{'json' if kind == 'inputs' else 'jsonl'}"
            for split in SPLITS
            for kind in ("inputs", "labels", "events", "event_labels")
        }
        _require(
            set(manifest["files"]) == expected_files,
            "Неверный перечень файлов манифеста",
        )
        for name, details in manifest["files"].items():
            content = (directory / name).read_bytes()
            _require(
                len(content) == details["bytes"]
                and hashlib.sha256(content).hexdigest() == details["sha256"],
                "Не совпадает контрольная сумма: " + name,
            )
        bundle = {
            key: manifest[key]
            for key in (
                "dataset_version",
                "schema_version",
                "seed",
                "size",
                "catalog_sha256",
            )
        }
        bundle["splits"] = {}
        for split in SPLITS:
            data = {
                "inputs": json.loads(
                    (directory / f"{split}.inputs.json").read_text(encoding="utf-8")
                )
            }
            for kind in ("labels", "events", "event_labels"):
                data[kind] = [
                    json.loads(line)
                    for line in (directory / f"{split}.{kind}.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
            bundle["splits"][split] = data
        validate_dataset(bundle)
        _require(
            manifest["counts"]
            == {s: len(d["inputs"]) for s, d in bundle["splits"].items()},
            "Неверные счетчики манифеста",
        )
        return bundle
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Набор поврежден или имеет неподдерживаемую структуру"
        ) from exc
