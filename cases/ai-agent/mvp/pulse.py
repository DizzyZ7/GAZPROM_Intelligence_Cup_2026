"""Offline deterministic TIIM Pulse baseline. No external calls."""
from collections import defaultdict
from dataclasses import dataclass
import json
import re
from pathlib import Path

PATTERNS = [
    ('bug', 'booking_queue', ('очеред', 'брониров'), ('404', 'ошибк', 'не существует')),
    ('bug', 'contract_next_button', ('договор', 'оферт'), ('кнопк', 'далее', 'не реагирует')),
    ('bug', 'vehicle_registry', ('машин', 'автомобил'), ('справочник', 'добав', 'исчез')),
    ('bug', 'sms_recovery', ('смс', 'sms', 'код'), ('парол', 'восстанов')),
    ('feature_request', 'contact_edit', ('телефон', 'номер'), ('измен', 'добав', 'контакт')),
]

@dataclass(frozen=True)
class Ticket:
    id: str
    product: str
    text: str
    channel: str = 'web'
    occurred_at: str | None = None

def classify(text: str) -> tuple[str, str, float]:
    t = text.casefold()
    for category, workflow, group_a, group_b in PATTERNS:
        if any(x in t for x in group_a) and any(x in t for x in group_b):
            return category, workflow, 0.75
    return 'unknown', 'unclassified', 0.0

def redact(text: str) -> str:
    text = re.sub(r'(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)', '[PHONE]', text)
    return re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)

def analyze(tickets: list[Ticket], threshold: int = 2) -> dict:
    groups = defaultdict(list)
    classified = []
    for ticket in tickets:
        category, workflow, score = classify(ticket.text)
        classified.append({'id': ticket.id, 'product': ticket.product, 'category': category,
                           'workflow': workflow, 'score': score, 'needs_review': category == 'unknown'})
        if category != 'unknown':
            groups[(ticket.product, category, workflow)].append(ticket)
    signals = []
    for (product, category, workflow), members in sorted(groups.items()):
        unique = {t.id: t for t in members}
        if len(unique) < threshold:
            continue
        signals.append({'id': f'{product}:{category}:{workflow}', 'product': product,
                        'category': category, 'workflow': workflow,
                        'ticket_count': len(unique), 'ticket_ids': sorted(unique),
                        'examples': [redact(t.text)[:180] for t in list(unique.values())[:2]],
                        'status': 'pending_human_review'})
    return {'processed': len(tickets), 'classified': classified, 'signals': signals,
            'disclaimer': 'Synthetic demonstration; rules-based baseline, not Qwen integration'}

def main() -> None:
    source = Path(__file__).with_name('sample_tickets.json')
    tickets = [Ticket(**row) for row in json.loads(source.read_text(encoding='utf-8'))]
    print(json.dumps(analyze(tickets), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
