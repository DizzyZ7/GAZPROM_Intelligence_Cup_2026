"""Офлайн-группировка обращений по эмбеддингам с консервативными границами."""
from collections import defaultdict
from dataclasses import dataclass
from pulse import Ticket, classify, redact
from semantic import match_pair
from issue_status import filter_resolved

@dataclass(frozen=True)
class EnrichedTicket:
    ticket: Ticket
    category: str
    workflow: str
    vector: list[float]

def analyze_semantic(tickets: list[Ticket], provider, threshold: float = 0.86, min_tickets: int = 2) -> dict:
    if not 0 < threshold <= 1 or min_tickets < 2:
        raise ValueError('Invalid thresholds')
    ids = [t.id for t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate ticket IDs: revise source before processing')
    if any(not t.id or not t.product or not t.text.strip() for t in tickets):
        raise ValueError('Ticket requires id, product and text')
    eligible, resolved = filter_resolved(tickets)
    vectors = provider.embed([redact(t.text) for t in eligible]) if eligible else []
    if len(vectors) != len(eligible):
        raise ValueError('Embedding provider returned wrong number of vectors')
    enriched = []
    for t, v in zip(eligible, vectors):
        category, workflow, _ = classify(t.text)
        enriched.append(EnrichedTicket(t, category, workflow, v))
    buckets = defaultdict(list)
    for item in enriched:
        if item.category != 'unknown':
            buckets[(item.ticket.product, item.category, item.workflow)].append(item)
    signals = []
    for (product, category, workflow), items in sorted(buckets.items()):
        clusters = []
        for item in sorted(items, key=lambda x: x.ticket.id):
            for cluster in clusters:
                if all(match_pair(
                    {'id': item.ticket.id, 'product': product, 'category': category, 'workflow': workflow},
                    {'id': other.ticket.id, 'product': product, 'category': category, 'workflow': workflow},
                    (item.vector, other.vector), threshold
                ).accepted for other in cluster):
                    cluster.append(item)
                    break
            else:
                clusters.append([item])
        for cluster in clusters:
            if len(cluster) < min_tickets:
                continue
            members = sorted(cluster, key=lambda x: x.ticket.id)
            signals.append({
                'id': f'{product}:{category}:{workflow}:{members[0].ticket.id}',
                'product': product, 'category': category, 'workflow': workflow,
                'ticket_count': len(members), 'ticket_ids': [x.ticket.id for x in members],
                'examples': [redact(x.ticket.text)[:180] for x in members[:2]],
                'status': 'pending_human_review', 'method': 'complete_link_embeddings',
                'threshold': threshold,
            })
    return {'processed': len(tickets), 'signals': signals,
            'resolved_tickets': resolved,
            'unclassified': sum(x.category == 'unknown' for x in enriched)}
