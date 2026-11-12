"""Единая рабочая группировка с проверяемыми ограничениями контекста."""

import hashlib
import json
from collections import defaultdict

from context_pipeline import _clusters, _embedding_vectors
from issue_status import has_explicit_resolution
from product_context import ANALYSIS_VERSION, classify_ticket
from pulse import Ticket, redact
from ticket_context import TicketContext

# Канонические ID существующих сценариев сохраняются; остальные ключи имеют суффикс.
_LEGACY_CONTEXTS = {
    ("booking_queue", "booking_queue", "http_404"),
    ("contract_next_button", "contract_next_button", "control_unresponsive"),
    ("vehicle_registry", "record_retention", "missing_record"),
    ("sms_recovery", "sms_recovery", "missing_delivery"),
    ("contact_edit", "contact_edit", "edit_contact"),
}


def analyze_product(tickets, *, provider=None, threshold=0.86, min_tickets=2):
    if (
        type(min_tickets) is not int
        or min_tickets < 2
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 < threshold <= 1
    ):
        raise ValueError("Некорректный порог или минимальное число обращений")
    if len({ticket.id for ticket in tickets}) != len(tickets):
        raise ValueError("Повторяющиеся идентификаторы обращений")
    if any(
        not isinstance(value, str) or not value.strip()
        for ticket in tickets
        for value in (ticket.id, ticket.product, ticket.text, ticket.channel)
    ):
        raise ValueError("Поля обращения должны быть непустыми строками")
    buckets = defaultdict(list)
    candidates = []
    classified = []
    resolved = 0
    for ticket in sorted(tickets, key=lambda item: item.id):
        safe = Ticket(ticket.id, ticket.product, redact(ticket.text), ticket.channel)
        context = classify_ticket(safe.text)
        if (
            context.category in {"bug", "unknown", "other"}
            and context.reason not in {"untrusted_instruction", "mixed_intents"}
            and has_explicit_resolution(safe.text)
        ):
            context = TicketContext("other", context.workflow, reason="resolved_issue")
            resolved += 1
        classified.append(
            {
                "id": ticket.id,
                "product": ticket.product,
                "category": context.category,
                "workflow": context.workflow,
                "operation": context.operation,
                "symptom": context.symptom,
                "reason": context.reason,
                "needs_review": context.category == "unknown"
                or (
                    context.category in {"bug", "feature_request"}
                    and not context.groupable
                ),
            }
        )
        if context.groupable:
            key = (
                ticket.product,
                context.category,
                context.workflow,
                context.operation,
                context.symptom,
            )
            buckets[key].append(safe)
            candidates.append(safe)
    vectors = _embedding_vectors(provider, candidates) if provider is not None else None
    signals = []
    for key, members in sorted(buckets.items()):
        product, category, workflow, operation, symptom = key
        prefix = f"{product}:{category}:{workflow}"
        if (workflow, operation, symptom) not in _LEGACY_CONTEXTS:
            digest = hashlib.sha256(
                json.dumps([operation, symptom], ensure_ascii=False).encode()
            ).hexdigest()[:16]
            prefix += ":" + digest
        for cluster in _clusters(members, vectors, threshold):
            if len(cluster) < min_tickets:
                continue
            signal = {
                "id": prefix if vectors is None else f"{prefix}:{cluster[0].id}",
                "product": product,
                "category": category,
                "workflow": workflow,
                "context": {"operation": operation, "symptom": symptom},
                "analysis_version": ANALYSIS_VERSION,
                "ticket_count": len(cluster),
                "ticket_ids": [ticket.id for ticket in cluster],
                "examples": [ticket.text[:180] for ticket in cluster[:2]],
                "status": "pending_human_review",
                "method": "context_rules"
                if vectors is None
                else "complete_link_embeddings",
            }
            if vectors is not None:
                signal["threshold"] = threshold
            signals.append(signal)
    return {
        "processed": len(tickets),
        "signals": signals,
        "classified": classified,
        "resolved_tickets": resolved,
        "unclassified": sum(row["category"] == "unknown" for row in classified),
    }
