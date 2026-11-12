"""Контекст поддерживаемых сценариев рабочего импорта."""

import re

from ticket_context import TicketContext, classify_context

ANALYSIS_VERSION = "product-context-v1"
_NOT_FOUND = re.compile(r"\bnot\s+found\b", re.IGNORECASE)
_NUMERIC_NOT_FOUND = re.compile(
    r"(?<![\w.])(?:http\s+)?404\s+not\s+found\b", re.IGNORECASE
)
_CONTACT = re.compile(r"телефон|контакт", re.IGNORECASE)
_SELF_EDIT = re.compile(r"самостоятельн\w*\s+смен\w*", re.IGNORECASE)
_SELF_EDIT_REQUEST = re.compile(
    r"\bможно\s+(?:ли\s+)?сделать\s+самостоятельн\w*\s+смен\w*\s+контактн\w*\s+телефон",
    re.IGNORECASE,
)
_DECLINED = re.compile(r"не\s+(?:нужн|требует|хочу|надо)", re.IGNORECASE)
_GUIDANCE = re.compile(r"\bкак\s+(?:выполнить|сделать|оформить)\b", re.IGNORECASE)
_BARE_STATUS = re.compile(
    r"\b(выходит|выдает|показывает|вижу)\s+([45]\d{2})(?=\s*(?:[.!;\n]|$))",
    re.IGNORECASE,
)
_COUNTER = re.compile(r"счетчик|количеств|число", re.IGNORECASE)
_ACCESS_REQUEST = re.compile(
    r"оформите\s+(?:заявк\w*|запрос)\s+на\s+(?:прав\w*|доступ)",
    re.IGNORECASE,
)

_MISSING_PAGE = re.compile(
    r"страниц[аы]\s+не\s+существует|отсутствующ\w*\s+страниц\w*", re.IGNORECASE
)
_RESERVE_CONTACT = re.compile(r"записать\s+еще\s+один\s+запасной", re.IGNORECASE)
_CLOSE_PREVIOUS = re.compile(
    r"(закрыть|закройте)\s+предыдущ\w*\s+(заявк\w*|обращен\w*|тикет\w*)", re.IGNORECASE
)
_NOTIFICATION = re.compile(r"уведомлен|напоминан|оповещен", re.IGNORECASE)
_NOTIFICATION_NEEDS = {
    "appointment_reminder": re.compile(
        r"до\s+приема|перед\s+(?:уже\s+)?(?:подтвержденн\w*\s+)?прием", re.IGNORECASE
    ),
    "slot_availability": re.compile(
        r"(?:появ\w*|свободн\w*)[^.!?;\n]{0,45}слот|слот[^.!?;\n]{0,45}появ",
        re.IGNORECASE,
    ),
    "queue_approaching": re.compile(r"очеред\w*[^.!?;\n]{0,35}близко", re.IGNORECASE),
}


def classify_ticket(text: str) -> TicketContext:
    # Это лексическая нормализация сообщения, а не предположение о HTTP-коде.
    normalized = _CLOSE_PREVIOUS.sub(r"\1 \2", text)
    if _CONTACT.search(text) and not _DECLINED.search(text):
        normalized = _RESERVE_CONTACT.sub(
            "добавьте второй контактный телефон", normalized
        )
    normalized = _MISSING_PAGE.sub("ошибка not_found", normalized)
    normalized = _NOT_FOUND.sub(
        "ошибка not_found", _NUMERIC_NOT_FOUND.sub("ошибка 404", normalized)
    )
    if not _COUNTER.search(text):
        normalized = _BARE_STATUS.sub(r"\1 ошибка \2", normalized)
    if not _DECLINED.search(text) and not _GUIDANCE.search(text):
        normalized = _SELF_EDIT_REQUEST.sub(
            "Хочу изменять контактный телефон", normalized
        )
    normalized = re.sub(
        r"(добавьте)\s+(уведомлен\w*|оповещен\w*)",
        r"\1 возможность получать \2",
        normalized,
        flags=re.IGNORECASE,
    )
    context = classify_context(normalized)
    if context.reason in {"untrusted_instruction", "mixed_intents", "missing_evidence"}:
        return context
    if context.reason == "ambiguous_workflow":
        return TicketContext("unknown", reason="ambiguous_workflow")
    if context.symptom == "app_not_found":
        context = TicketContext(
            context.category,
            context.workflow,
            context.operation,
            "not_found",
            context.reason,
        )
    if (
        context.category == "feature_request"
        and context.workflow == "booking_queue"
        and _NOTIFICATION.search(text)
    ):
        if _DECLINED.search(text):
            return TicketContext("unknown", reason="declined_notification_request")
        needs = [
            name
            for name, pattern in _NOTIFICATION_NEEDS.items()
            if pattern.search(text)
        ]
        if len(needs) != 1:
            return TicketContext(
                "feature_request", "booking_queue", reason="ambiguous_notification_need"
            )
        return TicketContext(
            "feature_request",
            "booking_queue",
            "booking_queue",
            needs[0],
            "context_resolved",
        )
    if context.category == "how_to":
        return context
    if context.category == "unknown" and _GUIDANCE.search(text):
        if _CONTACT.search(text) and _SELF_EDIT.search(text):
            return TicketContext("how_to", "contact_edit", reason="guidance_requested")
        if re.search(r"прав|доступ", text, re.IGNORECASE):
            return TicketContext("how_to", "permissions", reason="guidance_requested")
    if _ACCESS_REQUEST.search(text):
        if context.category in {"bug", "feature_request"}:
            return TicketContext("unknown", reason="mixed_intents")
        return TicketContext(
            "service_request", "permissions", reason="individual_request"
        )
    return context
