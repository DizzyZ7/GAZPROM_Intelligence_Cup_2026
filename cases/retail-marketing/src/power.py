"""Планирование A/B по средним литрам: аналитическая аппроксимация.

Не оценивает причинный эффект и не заменяет пилот. Не читает клиентские ID.
Гауссово приближение с независимыми клиентами и общей SD результата;
кластеризация, неодинаковая дисперсия и последовательные проверки требуют
более осторожного дизайна.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from math import ceil, isfinite, sqrt
from statistics import NormalDist


@dataclass(frozen=True)
class PowerPlan:
    eligible: int
    sample_share: float
    baseline_sd_liters: float
    target_effect_liters: float
    alpha: float = 0.05
    power: float = 0.8
    variance_reduction: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.eligible, int) or isinstance(self.eligible, bool) or self.eligible < 4:
            raise ValueError('Для расчета нужно не менее 4 клиентов')
        for k in ('sample_share', 'baseline_sd_liters', 'target_effect_liters', 'alpha', 'power', 'variance_reduction'):
            value = getattr(self, k)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
                raise ValueError(f'Параметр {k} должен быть конечным числом')
        if not (0 < self.sample_share <= 1):
            raise ValueError('Доля пилота должна быть больше нуля и не больше 1')
        if self.baseline_sd_liters <= 0 or self.target_effect_liters <= 0:
            raise ValueError('SD и целевой эффект должны быть положительными')
        if not (0 < self.alpha < 0.5) or not (0.5 < self.power < 1):
            raise ValueError('Проверяйте alpha и мощность')
        if not (0 <= self.variance_reduction < 1):
            raise ValueError('Предполагаемая доля сокращения дисперсии: [0,1)')
        if self.sample_count < 4:
            raise ValueError('Пилот слишком мал: менее 4 участников')

    @property
    def sample_count(self) -> int:
        return min(self.eligible, int(self.eligible * self.sample_share + 0.5))

    @property
    def arms(self) -> tuple[int, int]:
        n = self.sample_count
        return n // 2, n - n // 2

    @property
    def sd_after_adjustment(self) -> float:
        return self.baseline_sd_liters * sqrt(1 - self.variance_reduction)

    @property
    def critical_value(self) -> float:
        q = NormalDist()
        return q.inv_cdf(1 - self.alpha / 2) + q.inv_cdf(self.power)

    @property
    def standard_error(self) -> float:
        treatment, control = self.arms
        return self.sd_after_adjustment * sqrt(1/treatment + 1/control)

    @property
    def mde_liters(self) -> float:
        """Минимальный различимый эффект (80% мощности по умолчанию)."""
        return self.critical_value * self.standard_error

    @property
    def ci_halfwidth_liters(self) -> float:
        """Полуширина 95% ДИ для наблюдаемой разности средних при заданной SD."""
        return NormalDist().inv_cdf(1 - self.alpha / 2) * self.standard_error

    @property
    def required_per_arm(self) -> int:
        """Приближенное n на группу для желаемого эффекта, 1:1."""
        return ceil(2 * (self.critical_value * self.sd_after_adjustment / self.target_effect_liters)**2)

    def report(self) -> dict:
        treated, control = self.arms
        return {
            'status': 'сценарное планирование, НЕ результат эксперимента',
            'assumptions': asdict(self),
            'sample_total': self.sample_count,
            'treatment': treated,
            'control': control,
            'mde_liters': round(self.mde_liters, 3),
            'ci_halfwidth_liters': round(self.ci_halfwidth_liters, 3),
            'required_per_arm_for_target': self.required_per_arm,
            'required_total_for_target': 2 * self.required_per_arm,
            'enough_candidates_for_target': 2 * self.required_per_arm <= self.eligible,
            'caveat': 'SD — исторический ориентир, не известная будущая дисперсия; кластеризация, сезонность и иные эффекты могут увеличить MDE.',
        }


def main() -> None:
    ap = argparse.ArgumentParser(description='План мощности A/B по литрам — сценарная оценка')
    ap.add_argument('--eligible', type=int, default=4050)
    ap.add_argument('--sample-share', type=float, default=0.10)
    ap.add_argument('--baseline-sd-liters', type=float, default=50.71,
                    help='Ориентир: SD среднего объема за март—май 2024 (подгруппа 4050)')
    ap.add_argument('--target-effect-liters', type=float, default=3.0)
    ap.add_argument('--alpha', type=float, default=0.05)
    ap.add_argument('--power', type=float, default=0.80)
    ap.add_argument('--variance-reduction', type=float, default=0.0,
                    help='Гипотетическая доля уменьшения дисперсии после ковариатной коррекции')
    a = ap.parse_args()
    plan = PowerPlan(a.eligible, a.sample_share, a.baseline_sd_liters,
                     a.target_effect_liters, a.alpha, a.power, a.variance_reduction)
    print(json.dumps(plan.report(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()