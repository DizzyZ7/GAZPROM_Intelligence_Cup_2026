"""Рабочий семантический pipeline с обязательными ограничениями контекста."""

from product_analysis import analyze_product
from pulse import Ticket


def analyze_semantic(
    tickets: list[Ticket], provider, threshold: float = 0.86, min_tickets: int = 2
) -> dict:
    if provider is None:
        raise ValueError("Семантический анализ требует провайдер эмбеддингов")
    return analyze_product(
        tickets, provider=provider, threshold=threshold, min_tickets=min_tickets
    )
