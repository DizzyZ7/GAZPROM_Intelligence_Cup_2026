"""Калибровка только validation и финальная оценка по зафиксированному протоколу."""

import json
import platform
import re
import time
from fractions import Fraction
from pathlib import Path
from urllib.error import URLError

from pulse import Ticket, redact

from .artifact_io import csv_bytes, json_bytes, read_json, sha256, write_directory
from .engine import MAX_PAIRS, METHODS, analyze_method, apply_threshold
from .metrics import CATEGORIES
from .reports import comparison_files, method_files, score_analysis
from .tfidf import TfidfModel

VERSION = "pulse-benchmark-v1"
GRID = tuple(round(i / 20, 2) for i in range(1, 20))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def source_fingerprints():
    root = Path(__file__).resolve().parent.parent
    files = [
        root / "pulse.py",
        root / "semantic.py",
        root / "ollama_embeddings.py",
    ] + sorted((root / "benchmark").glob("*.py"))
    return {str(path.relative_to(root)): sha256(path.read_bytes()) for path in files}


class DatasetReader:
    def __init__(self, directory):
        self.root = Path(directory)
        self.manifest = read_json(self.root / "manifest.json")
        self.fingerprint = sha256((self.root / "manifest.json").read_bytes())
        _require(
            self.manifest.get("schema_version") == 1
            and isinstance(self.manifest.get("files"), dict),
            "Неизвестная схема набора",
        )

    def read_split(self, split, gold=True):
        _require(
            split in ("development", "validation", "holdout"), "Неизвестная выборка"
        )
        names = [f"{split}.inputs.json"] + ([f"{split}.labels.jsonl"] if gold else [])
        loaded = []
        for name in names:
            details = self.manifest["files"].get(name, {})
            content = (self.root / name).read_bytes()
            _require(
                sha256(content) == details.get("sha256")
                and len(content) == details.get("bytes"),
                "Контрольная сумма выборки не совпадает",
            )
            loaded.append(
                json.loads(content)
                if name.endswith(".json")
                else [json.loads(line) for line in content.splitlines()]
            )
        sources = loaded[0]
        _require(
            isinstance(sources, list)
            and len(sources) == self.manifest["counts"][split],
            "Размер выборки не соответствует манифесту",
        )
        _require(
            not gold or len(sources) * (len(sources) - 1) // 2 <= MAX_PAIRS,
            "Выборка превышает лимит числа пар benchmark",
        )
        identities = set()
        for row in sources:
            _require(
                isinstance(row, dict)
                and set(row) == {"id", "product", "text", "channel"},
                "В признаках запрещены gold и дополнительные поля",
            )
            _require(
                all(
                    isinstance(v, str) and v.strip() and len(v) <= limit
                    for v, limit in (
                        (row["id"], 128),
                        (row["product"], 128),
                        (row["text"], 10000),
                        (row["channel"], 64),
                    )
                ),
                "Неверный признак обращения",
            )
            _require(row["id"] not in identities, "Повторный ID в выборке")
            identities.add(row["id"])
        if not gold:
            return sources, []
        labels = loaded[1]
        _require(
            len(labels) == len(sources)
            and {r["ticket_id"] for r in labels} == identities,
            "Эталоны не соответствуют обращениям",
        )
        by_id = {row["id"]: row for row in sources}
        for row in labels:
            source = by_id[row["ticket_id"]]
            _require(
                row["category"] in CATEGORIES
                and row["product_id"] == source["product"]
                and row["source_channel"] == source["channel"],
                "Неверная эталонная категория или источник",
            )
            _require(
                row["gold_cluster_id"] is None
                or (
                    isinstance(row["gold_cluster_id"], str)
                    and bool(row["gold_cluster_id"].strip())
                ),
                "Неверная эталонная группа",
            )
        return sources, labels


def _descriptor(value):
    _require(
        isinstance(value, dict)
        and set(value) == {"provider", "model", "revision"}
        and all(
            isinstance(v, str) and v.strip() and len(v) <= 256 for v in value.values()
        ),
        "Для модели необходимы provider, model и revision",
    )
    return dict(value)


def _select(analysis, sources, labels):
    rows, best, selected, best_metrics = [], None, None, None
    for threshold in GRID:
        quality, _, _ = score_analysis(
            apply_threshold(analysis, threshold), sources, labels
        )
        metrics = quality["clustering"]
        rows.append(
            {
                "method": analysis["method"],
                "threshold": threshold,
                **{
                    k: metrics[k]
                    for k in ("precision", "recall", "f1", "tp", "fp", "fn")
                },
            }
        )
        denominator = 2 * metrics["tp"] + metrics["fp"] + metrics["fn"]
        exact_f1 = (
            Fraction(2 * metrics["tp"], denominator) if denominator else Fraction(0)
        )
        candidate = (exact_f1, -metrics["fp"], threshold)
        if best is None or candidate > best:
            best, selected = candidate, threshold
            best_metrics = metrics
    positives = best_metrics["tp"] + best_metrics["fn"]
    info = {
        "calibration_status": (
            "no_positive_gold"
            if not positives
            else ("no_true_positives" if not best_metrics["tp"] else "selected")
        ),
        "best_validation_f1": best_metrics["f1"],
        "best_validation_tp": best_metrics["tp"],
        "gold_positive_pairs": positives,
    }
    return selected, rows, info


def calibrate(
    dataset_dir, output, provider=None, descriptor=None, unavailable_reason=None
):
    if Path(output).exists():
        raise FileExistsError("Калибровка уже существует")
    if provider is not None:
        descriptor = _descriptor(descriptor)
    reader = DatasetReader(dataset_dir)
    development, _ = reader.read_split("development", gold=False)
    sources, labels = reader.read_split("validation")
    started = time.perf_counter()
    tfidf = TfidfModel.fit([redact(row["text"]) for row in development])
    fit_seconds = time.perf_counter() - started
    state = json_bytes(tfidf.to_state())
    tickets = [Ticket(**row) for row in sources]
    initial = analyze_method("tfidf", tickets, threshold=GRID[0], tfidf=tfidf)
    threshold, rows, selection = _select(initial, sources, labels)
    methods = {
        "rules": {"status": "ready", "threshold": None},
        "tfidf": {"status": "ready", "threshold": threshold, **selection},
        "embeddings": {
            "status": "not_checked",
            "reason": unavailable_reason
            or "Локальный embedding-provider не задан; инференс не выполнялся",
        },
    }
    if provider is not None:
        try:
            initial = analyze_method(
                "embeddings", tickets, threshold=GRID[0], provider=provider
            )
            semantic_threshold, semantic_rows, semantic_selection = _select(
                initial, sources, labels
            )
            rows += semantic_rows
            methods["embeddings"] = {
                "status": "ready",
                "threshold": semantic_threshold,
                "descriptor": descriptor,
                **semantic_selection,
            }
        except (ValueError, RuntimeError, OSError, URLError) as exc:
            methods["embeddings"] = {
                "status": "failed",
                "reason": "Ошибка провайдера при калибровке: " + type(exc).__name__,
                "descriptor": descriptor,
            }
    config = {
        "schema_version": 1,
        "benchmark_version": VERSION,
        "dataset_manifest_sha256": reader.fingerprint,
        "dataset_version": reader.manifest["dataset_version"],
        "selection_split": "validation",
        "selection_objective": "clustering.pairwise_f1; затем меньше fp; затем больший порог",
        "threshold_grid": list(GRID),
        "top_k": 5,
        "max_pairs": MAX_PAIRS,
        "source_fingerprints": source_fingerprints(),
        "tfidf_state_sha256": sha256(state),
        "methods": methods,
    }
    config["configuration_sha256"] = sha256(json_bytes(config))
    write_directory(
        output,
        {
            "frozen-config.json": json_bytes(config),
            "tfidf-state.json": state,
            "validation-grid.csv": csv_bytes(
                ("method", "threshold", "precision", "recall", "f1", "tp", "fp", "fn"),
                rows,
            ),
            "calibration-performance.json": json_bytes(
                {
                    "tfidf_fit_seconds": fit_seconds,
                    "development_count": len(development),
                    "validation_count": len(sources),
                }
            ),
        },
    )
    return config


def _load_frozen(reader, calibration_dir):
    root = Path(calibration_dir)
    config = read_json(root / "frozen-config.json")
    unsigned = {k: v for k, v in config.items() if k != "configuration_sha256"}
    _require(
        config.get("configuration_sha256") == sha256(json_bytes(unsigned)),
        "Зафиксированные параметры изменены",
    )
    _require(
        config.get("benchmark_version") == VERSION
        and config.get("schema_version") == 1
        and config.get("selection_split") == "validation"
        and config.get("threshold_grid") == list(GRID)
        and config.get("top_k") == 5
        and config.get("max_pairs") == MAX_PAIRS,
        "Неизвестный протокол оценки",
    )
    _require(
        config["dataset_manifest_sha256"] == reader.fingerprint,
        "Набор отличается от использованного при калибровке",
    )
    _require(
        config["source_fingerprints"] == source_fingerprints(),
        "Код изменен после фиксации параметров",
    )
    _require(
        set(config["methods"]) == set(METHODS)
        and config["methods"]["tfidf"]["threshold"] in GRID,
        "Неверные методы или порог",
    )
    state = (root / "tfidf-state.json").read_bytes()
    _require(sha256(state) == config["tfidf_state_sha256"], "Состояние TF-IDF изменено")
    return config, TfidfModel.from_state(json.loads(state))


def evaluate(dataset_dir, calibration_dir, output, provider=None, descriptor=None):
    output, calibration_dir = Path(output), Path(calibration_dir)
    if output.exists():
        raise FileExistsError("Отчет уже существует")
    reader = DatasetReader(dataset_dir)
    config, tfidf = _load_frozen(reader, calibration_dir)
    semantic = config["methods"]["embeddings"]
    if semantic["status"] == "ready":
        _require(
            provider is not None and _descriptor(descriptor) == semantic["descriptor"],
            "Нужен тот же embedding-provider и та же версия модели",
        )
    else:
        _require(
            provider is None, "Нельзя подключать новый метод после фиксации протокола"
        )
    receipt = calibration_dir / "holdout-receipt.json"
    with receipt.open("x", encoding="utf-8") as stream:
        stream.write(
            json_bytes(
                {
                    "status": "started",
                    "configuration_sha256": config["configuration_sha256"],
                }
            ).decode()
        )
    try:
        sources, labels = reader.read_split("holdout")
        tickets = [Ticket(**row) for row in sources]
        files, reports, performance = {}, {}, {}
        for method in METHODS:
            settings = config["methods"][method]
            analysis, missing = None, settings
            if settings["status"] == "ready":
                try:
                    analysis = analyze_method(
                        method,
                        tickets,
                        threshold=settings["threshold"],
                        tfidf=tfidf,
                        provider=provider,
                    )
                    performance[method] = analysis["performance"]
                except (ValueError, RuntimeError, OSError, URLError) as exc:
                    if method != "embeddings":
                        raise
                    missing = {
                        "status": "failed",
                        "reason": "Ошибка провайдера при финальной оценке: "
                        + type(exc).__name__,
                    }
            partial, report = method_files(
                method, analysis, sources, labels, k=config["top_k"], missing=missing
            )
            files.update(partial)
            reports[method] = report
        files.update(comparison_files(reports, performance))
        files["frozen-config.json"] = json_bytes(config)
        summary = {
            "schema_version": 1,
            "benchmark_version": VERSION,
            "status": "complete"
            if all(r["status"] == "ok" for r in reports.values())
            else "partial",
            "evaluation_split": "holdout",
            "configuration_sha256": config["configuration_sha256"],
            "dataset_manifest_sha256": reader.fingerprint,
            "methods": {m: r["status"] for m, r in reports.items()},
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "files": {
                name: {"sha256": sha256(value), "bytes": len(value)}
                for name, value in sorted(files.items())
            },
        }
        files["manifest.json"] = json_bytes(summary)
        write_directory(output, files)
        receipt.write_bytes(
            json_bytes(
                {
                    "status": "completed",
                    "configuration_sha256": config["configuration_sha256"],
                    "report_manifest_sha256": sha256(files["manifest.json"]),
                }
            )
        )
        return summary
    except BaseException:
        receipt.write_bytes(
            json_bytes(
                {
                    "status": "failed",
                    "configuration_sha256": config["configuration_sha256"],
                }
            )
        )
        raise


def verify_report(directory):
    root = Path(directory)
    manifest = read_json(root / "manifest.json")
    _require(
        manifest.get("benchmark_version") == VERSION
        and isinstance(manifest.get("files"), dict),
        "Неизвестный отчет",
    )
    _require(
        manifest.get("schema_version") == 1
        and manifest.get("evaluation_split") == "holdout",
        "Неверная схема или выборка отчета",
    )
    allowed = {"comparison.csv", "comparison.md", "frozen-config.json"}
    required = set(allowed)
    _require(
        set(manifest.get("methods", {})) == set(METHODS),
        "Не все методы отражены в отчете",
    )
    expected_status = (
        "complete"
        if all(status == "ok" for status in manifest["methods"].values())
        else "partial"
    )
    _require(
        manifest.get("status") == expected_status,
        "Итоговый статус не соответствует методам",
    )
    for method in METHODS:
        status = manifest["methods"][method]
        _require(status in {"ok", "not_checked", "failed"}, "Неизвестный статус метода")
        report = read_json(root / method / "report.json")
        _require(
            report.get("method") == method and report.get("status") == status,
            "Статусы отчета не согласованы",
        )
        tasks = ("classification", "retrieval_pairs", "retrieval_top_k", "clustering")
        _require(
            all(
                isinstance(report.get(task), dict)
                if status == "ok"
                else report.get(task) is None
                for task in tasks
            ),
            "Нет раздельных результатов задач",
        )
        required.update(
            method + "/" + name
            for name in (
                (
                    "report.json",
                    "performance.json",
                    "errors.json",
                    "groups.json",
                    "predictions.csv",
                    "confusion.csv",
                    "retrieval.csv",
                )
                if status == "ok"
                else ("report.json", "performance.json")
            )
        )
        allowed.update(
            method + "/" + name
            for name in (
                "report.json",
                "performance.json",
                "errors.json",
                "groups.json",
                "predictions.csv",
                "confusion.csv",
                "retrieval.csv",
            )
        )
    _require(
        set(manifest["files"]) == required,
        "Отчет неполон или содержит лишние артефакты",
    )
    for name, details in manifest["files"].items():
        _require(name in allowed, "Неизвестный файл отчета")
        content = (root / name).read_bytes()
        _require(
            len(content) == details["bytes"] and sha256(content) == details["sha256"],
            "Отчет поврежден: " + name,
        )
    _require(
        {"comparison.csv", "comparison.md", "frozen-config.json"}.issubset(
            manifest["files"]
        )
        and all(method + "/report.json" in manifest["files"] for method in METHODS),
        "Отчет неполон",
    )
    config = read_json(root / "frozen-config.json")
    unsigned = {k: v for k, v in config.items() if k != "configuration_sha256"}
    _require(
        sha256(json_bytes(unsigned))
        == config.get("configuration_sha256")
        == manifest.get("configuration_sha256"),
        "Нарушена фиксация параметров отчета",
    )
    _require(
        config.get("schema_version") == 1
        and config.get("benchmark_version") == VERSION
        and config.get("selection_split") == "validation"
        and config.get("dataset_manifest_sha256")
        == manifest.get("dataset_manifest_sha256"),
        "Атрибуция отчета не соответствует протоколу",
    )
    return {
        "status": "valid",
        "configuration_sha256": manifest["configuration_sha256"],
        "source_matches_current": config["source_fingerprints"]
        == source_fingerprints(),
    }


def local_ollama(model):
    """Локальная модель фиксируется по digest; транспорт использует запрет proxy/redirect."""
    from ollama_embeddings import OllamaEmbeddingProvider, urlopen

    provider = OllamaEmbeddingProvider(model=model)
    with urlopen(
        provider.url.removesuffix("/api/embed") + "/api/tags", timeout=3
    ) as response:
        payload = json.load(response)
    names = {model, model + ":latest"}
    selected = next(
        (row for row in payload.get("models", []) if row.get("name") in names), None
    )
    _require(
        selected is not None
        and re.fullmatch(r"[0-9a-f]{64}", selected.get("digest", "")) is not None,
        "Локальная модель или ее digest отсутствует",
    )
    return provider, {
        "provider": "ollama-loopback",
        "model": selected["name"],
        "revision": selected["digest"],
    }
