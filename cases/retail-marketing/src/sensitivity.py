"""Воспроизводимая чувствительность сегментации и сценарные офферы.

Использует только агрегированные отчеты. Сырые клиентские ID в результатах не сохраняет.
Пороговые сценарии не являются оценкой причинного эффекта.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from economics import OfferScenario

DEFAULT_HIGH = -0.08
DEFAULT_MODERATE = -0.025
SCENARIOS = (("мягче на 20%", 0.8), ("базовый", 1.0), ("строже на 20%", 1.2))


def classify(score, eligible, high, moderate):
    """Политика присвоения категорий, эквивалентная segment_one на базовых границах."""
    score = np.asarray(score, dtype=np.float64)
    eligible = np.asarray(eligible, dtype=bool)
    if score.shape != eligible.shape or score.ndim != 1:
        raise ValueError("Score и eligible должны быть одномерными массивами одной длины")
    if not np.isfinite(score).all() or not np.isfinite([high, moderate]).all():
        raise ValueError("Индексы и пороги должны быть конечными")
    if high >= moderate or high >= 0 or moderate >= 0:
        raise ValueError("Ожидаются high < moderate < 0")
    return np.where(~eligible, 3, np.where(score <= high, 0, np.where(score <= moderate, 1, 2)))


def segment_stress(score, eligible, push):
    score = np.asarray(score, dtype=np.float64)
    eligible = np.asarray(eligible, dtype=bool)
    push = np.asarray(push, dtype=bool)
    if score.shape != eligible.shape or score.shape != push.shape:
        raise ValueError("Длины входных векторов не совпадают")
    base = classify(score, eligible, DEFAULT_HIGH, DEFAULT_MODERATE)
    result = []
    for label, multiplier in SCENARIOS:
        high = DEFAULT_HIGH * multiplier
        moderate = DEFAULT_MODERATE * multiplier
        s = classify(score, eligible, high, moderate)
        counts = np.bincount(s.astype(int), minlength=4)
        targeted = (s == 0) | (s == 1)
        result.append({
            "scenario": label,
            "threshold_multiplier": multiplier,
            "high_threshold": round(high, 6),
            "moderate_threshold": round(moderate, 6),
            "high_signal": int(counts[0]),
            "moderate_signal": int(counts[1]),
            "no_signal": int(counts[2]),
            "insufficient_data": int(counts[3]),
            "candidates": int(targeted.sum()),
            "target_with_historical_push": int((targeted & push).sum()),
            "changed_segment_vs_baseline": int((s != base).sum()),
        })
    if not (result[0]["candidates"] >= result[1]["candidates"] >= result[2]["candidates"]):
        raise AssertionError("При ужесточении порогов аудитория неожиданно выросла")
    return {"method": "joint scaling of the two negative thresholds, fixed eligibility and scores",
            "disclaimer": "Robustness of an observational segmentation rule, not price elasticity or a campaign uplift",
            "customers": int(score.size), "scenarios": result}


def offer_stress(margin=9.94, basket=40.0, redemption=0.4,
                 communication=0.10, uplifts=(0, 0.5, 1, 3, 5), discounts=(0.30, 0.50, 1.0)):
    """Варианты затрат; выбранные вводные не измерены в эксперименте."""
    rows = []
    for discount in discounts:
        for uplift in uplifts:
            s = OfferScenario(float(margin), float(uplift), float(discount), float(basket),
                              float(redemption), float(communication))
            rows.append({"bonus_rub_per_liter": round(float(discount), 2),
                         "assumed_uplift_liters": float(uplift),
                         "reward_cost_rub": round(s.expected_reward_cost_rub, 2),
                         "break_even_liters": round(s.break_even_liters, 3),
                         "scenario_profit_rub_per_treatment": round(s.incremental_profit_rub, 2),
                         "scenario_positive": s.incremental_profit_rub > 0})
    return {"status": "hypothetical inputs, NOT experiment results",
            "assumptions": {"margin_rub_per_liter": margin, "basket_liters": basket,
                            "redemption_probability": redemption,
                            "communication_cost_rub": communication,
                            "opex_note": "Margin is illustrative and depends on approved OPEX accounting."},
            "rows": rows}


def save_stress(path, segment_report, economics_report):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"segmentation": segment_report,
                                  "bonus_economics": economics_report}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Маржрут: сценарная экономика офферов")
    p.add_argument("--margin", type=float, default=9.94)
    p.add_argument("--basket", type=float, default=40.0)
    p.add_argument("--redemption", type=float, default=0.4)
    p.add_argument("--communication", type=float, default=0.1)
    a = p.parse_args()
    print(json.dumps(offer_stress(a.margin, a.basket, a.redemption, a.communication),
                     ensure_ascii=False, indent=2))