"""Проверка явного завершения сбоя в рабочем импорте."""

import re

from pulse import Ticket, classify

_ISSUE = r"(?:сбо[йи]|ошибк[а-я]*|проблем[а-я]*|неполадк[а-я]*|дефект[а-я]*|баг[а-я]*)"
_DONE = r"(?:исправ(?:или|лен[аоы]?)|устран(?:или|ен[аоы]?))"
_RESOLUTION = (
    rf"(?:{_ISSUE}(?:\s+(?:уже|полностью|успешно|наконец|был[аои]?)){{0,3}}\s+{_DONE}"
    rf"|(?:исправили|устранили)\s+(?:(?:этот|эту|эти)\s+)?{_ISSUE}"
    rf"|{_ISSUE}\s+больше\s+не\s+(?:возникает|повторяется|воспроизводится|наблюдается))"
    r"(?:\s+(?:вчера|сегодня|утром|вечером))?"
)
# Только законченные положительные фразы. Неизвестный хвост остается в анализе.
_SUCCESS = (
    r"(?:спасибо|благодарю|(?:теперь\s+)?(?:все\s+(?:работает|хорошо|нормально)"
    r"|бронирование\s+работает|повторные\s+письма\s+приходят"
    r"|заявк[ау]\s+закры(?:та|ли)))"
)
_CLOSURE = re.compile(rf"{_RESOLUTION}(?:\s*[,—–-]\s*{_SUCCESS})*")
_ACKNOWLEDGEMENT = re.compile(_SUCCESS)


def has_explicit_resolution(text: str) -> bool:
    """Проверить последнее содержательное утверждение по узкой грамматике.

    Учитываются целые фразы, а не совпадение внутри вопроса, пересказа, отрицания
    или условия. Последующий текст должен быть известным подтверждением успеха
    или благодарностью. Все остальные конструкции остаются в обычном анализе.
    Это эвристика актуальности, а не проверка фактического состояния системы.
    """
    normalized = text.casefold().replace("ё", "е")
    # Перенос строки и точка с запятой не снимают отрицание или условие слева.
    fragments = re.split(r"[.!]+", normalized)
    for fragment in reversed(fragments):
        fragment = fragment.strip()
        if not fragment or _ACKNOWLEDGEMENT.fullmatch(fragment):
            continue
        return _CLOSURE.fullmatch(fragment) is not None
    return False


def filter_resolved(tickets: list[Ticket]) -> tuple[list[Ticket], int]:
    """Сохранение источников выполняет хранилище; здесь отбираются участники групп."""
    eligible = [
        ticket
        for ticket in tickets
        if classify(ticket.text)[0] != "bug" or not has_explicit_resolution(ticket.text)
    ]
    return eligible, len(tickets) - len(eligible)
