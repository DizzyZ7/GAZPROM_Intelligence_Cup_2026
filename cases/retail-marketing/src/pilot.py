"""Планировщик групп A/B для будущего пилота, без хранилища и отправки сообщений.

Секрет эксперимента не фиксируется в репозитории. ID используются только в памяти.
"""
from __future__ import annotations
import hmac
import hashlib
from math import isfinite


def assign_arm(customer_id: str, experiment_id: str, secret: str,
               treatment_share: float = 0.5) -> str:
    """Детерминированная псевдослучайная разбивка без публикации клиентских ID."""
    if not all([customer_id, experiment_id, secret]):
        raise ValueError('Необходимы ID клиента, ID пилота и секрет')
    if not 0.0 < treatment_share < 1.0:
        raise ValueError('Размер treatment должен быть строго между 0 и 1')
    message = (experiment_id + ':' + customer_id).encode('utf-8')
    digest = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).digest()
    quantile = int.from_bytes(digest[:8], 'big') / 2**64
    return 'treatment' if quantile < treatment_share else 'control'


def relative_uplift(treatment_liters: float, treatment_count: int,
                    control_liters: float, control_count: int) -> float:
    """Разница средних литров между группами; стратификация/CI нужны на пилоте."""
    if min(treatment_count, control_count) <= 0:
        raise ValueError('Нужны обе группы')
    if not all(isfinite(float(v)) for v in (treatment_liters, control_liters)):
        raise ValueError('Объемы должны быть конечными числами')
    if min(treatment_liters, control_liters) < 0:
        raise ValueError('Объемы не могут быть отрицательными')
    return treatment_liters / treatment_count - control_liters / control_count