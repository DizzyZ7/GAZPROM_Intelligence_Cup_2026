"""Сценарная экономика персонального оффера; это НЕ оцененный uplift.

Исходный конкурсный файл не содержит результатов скидочной кампании.
Каждое входное значение здесь — заданный сценарий, а не измеренный эффект.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class OfferScenario:
    margin_rub_per_liter: float
    assumed_incremental_liters: float
    reward_rub_per_liter: float
    eligible_basket_liters: float
    redemption_probability: float
    communication_cost_rub: float = 0.0

    def __post_init__(self):
        d = vars(self)
        if not all(isfinite(float(value)) for value in d.values()):
            raise ValueError('Параметры сценария должны быть конечными числами')
        if any(value < 0 for value in d.values()):
            raise ValueError('Отрицательные объемы, цены и затраты недопустимы')
        if self.margin_rub_per_liter <= 0:
            raise ValueError('Маржа на литр должна быть положительной')
        if self.redemption_probability > 1:
            raise ValueError('Вероятность использования бонуса от 0 до 1')

    @property
    def expected_reward_cost_rub(self) -> float:
        # Стоимость скидки на весь купонный чек, а не только на прирост литров.
        return self.redemption_probability * self.reward_rub_per_liter * self.eligible_basket_liters

    @property
    def incremental_profit_rub(self) -> float:
        return (self.assumed_incremental_liters * self.margin_rub_per_liter
                - self.expected_reward_cost_rub - self.communication_cost_rub)

    @property
    def break_even_liters(self) -> float:
        return (self.expected_reward_cost_rub + self.communication_cost_rub) / self.margin_rub_per_liter


def price_to_liter_margin(margin_rub_per_ton: float, opex_rub_per_ton: float,
                          density_kg_per_liter: float, subtract_opex: bool) -> float:
    """Учетная трактовка OPEX выбирается явно: double-count запрещен по умолчанию."""
    if not all(isfinite(float(v)) for v in (margin_rub_per_ton, opex_rub_per_ton, density_kg_per_liter)):
        raise ValueError('Финансовые показатели должны быть конечными числами')
    if min(margin_rub_per_ton, opex_rub_per_ton, density_kg_per_liter) < 0:
        raise ValueError('Отрицательный финансовый показатель')
    if density_kg_per_liter <= 0 or density_kg_per_liter >= 2:
        raise ValueError('Неверная плотность')
    base = margin_rub_per_ton - opex_rub_per_ton if subtract_opex else margin_rub_per_ton
    if base <= 0:
        raise ValueError('Отрицательный или нулевой вклад на литр')
    return base * density_kg_per_liter / 1000


def can_offer(consent_now: bool, has_app_now: bool, scenario: OfferScenario,
              verified_by_experiment: bool = False) -> bool:
    """По умолчанию отправка запрещена: исторический сигнал не равен причинному uplift."""
    return bool(consent_now and has_app_now and verified_by_experiment
                and scenario.incremental_profit_rub > 0)