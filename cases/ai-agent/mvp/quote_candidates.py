"""Офлайн-поиск кандидатов по двум подтвержденным цитатам."""

import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import chain, combinations
from math import isfinite

from benchmark.artifact_io import sha256
from benchmark.engine import _validated_vectors
from benchmark.metrics import CATEGORIES
from semantic import EmbeddingProvider, cosine
from span_context import _ERROR_CODE, extract_spans, normalize_fragment, safe_text
from structured_context import _CLAUSE, MAX_TEXT_CHARS, _normalized
from ticket_context import ACTIONABLE

VERSION = "quote-candidates-v1"
TOP_K = 3
MAX_TICKETS = 96
BATCH_SIZE = 32
_FIELDS = {"ticket_id", "category", "operation_quote", "symptom_quote"}
_NEGATOR = re.compile(r"\b(?:не|нет|без|ни|никак|никогда)\b", re.IGNORECASE)
_NEGATED_WORD = re.compile(r"\s+([а-яa-z0-9]+)")
_SCOPE_PREFIX = {
    "сразу",
    "всегда",
    "совсем",
    "полностью",
    "вовсе",
    "даже",
    "больше",
    "могу",
    "может",
    "могут",
    "могла",
    "могли",
    "удается",
    "удалось",
    "должен",
    "должна",
    "должны",
    "будет",
    "будут",
}
_CODE_TOKEN = re.compile(
    r"(?<![\w-])([a-z]{1,12}[-_]\d{1,8}|(?:err|app|e)\d{2,8}|"
    r"ERR(?:_[A-Z0-9]+)+)(?![\w-])",
    re.IGNORECASE,
)
_HTTP_VERSION_CODE = re.compile(r"\bHTTP/\d+(?:\.\d+)?\s+([45]\d{2})\b", re.IGNORECASE)
_CODE_SUFFIX = re.compile(r"(?:[.:/_-][a-zA-Z0-9]+)*")
_WORD = re.compile(r"[^\W_]")
_UNICODE_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[^\W\d_]{2,63}\b")
_LOCAL_PHONE = re.compile(
    r"(?<!\w)\(?\d{3}\)?[ \t.-]*\d{3}[ \t.-]*\d{2}[ \t.-]*\d{2}(?!\w)"
)


@dataclass(frozen=True)
class QuoteEvidence:
    ticket_id: str
    product: str
    category: str
    operation: str
    symptom: str
    source_sha256: str
    operation_span: tuple[int, int]
    symptom_span: tuple[int, int]
    error_code: str | None
    negation: tuple[str, ...]


def _negation(fragment):
    text = _normalized(fragment)
    signatures = []
    for negator in _NEGATOR.finditer(text):
        anchor, prefix = _NEGATED_WORD.match(text, negator.end()), []
        while anchor and anchor[1] in _SCOPE_PREFIX and len(prefix) < 2:
            prefix.append(anchor[1])
            anchor = _NEGATED_WORD.match(text, anchor.end())
        if (
            anchor is None
            or anchor[1] in _SCOPE_PREFIX
            or _NEGATOR.fullmatch(anchor[1])
        ):
            return None
        signatures.append(
            " ".join((negator[0], *prefix, normalize_fragment(anchor[1])))
        )
    return tuple(signatures)


def candidate_text(text):
    return _LOCAL_PHONE.sub("[PHONE]", _UNICODE_EMAIL.sub("[EMAIL]", safe_text(text)))


def _code_occurrences(text):
    for match in chain(
        _ERROR_CODE.finditer(text),
        _CODE_TOKEN.finditer(text),
        _HTTP_VERSION_CODE.finditer(text),
    ):
        start, end = match.span(1)
        end = _CODE_SUFFIX.match(text, end).end()
        yield text[start:end], start, end


def _guard_negation(text, operation_span, symptom_span, symptom_negation):
    spans = (operation_span, symptom_span)
    left, right = min(span[0] for span in spans), max(span[1] for span in spans)
    start = max((m.end() for m in _CLAUSE.finditer(text, 0, left)), default=0)
    boundary = _CLAUSE.search(text, right - 1)
    end = boundary.start() if boundary else len(text)
    if any(
        not any(a <= marker.start() and marker.end() <= b for a, b in spans)
        for marker in _NEGATOR.finditer(text, start, end)
    ):
        return "uncovered_negation"
    if symptom_negation:
        tail_start = symptom_span[1]
        boundary = _CLAUSE.search(text, tail_start - 1)
        tail_end = boundary.start() if boundary else len(text)
        if operation_span[0] >= tail_start:
            tail_end = min(tail_end, operation_span[0])
        if _WORD.search(text, tail_start, tail_end):
            return "incomplete_negative_scope"
    return None


def bind_evidence(sources, proposals):
    """Связывает подтвержденные фрагменты с источником перед допуском в поиск."""
    by_id = {row["id"]: row for row in sources}
    if len(by_id) != len(sources) or len(sources) > MAX_TICKETS:
        raise ValueError("Повтор ID или превышен лимит обращений")
    evidence, reasons, seen = {}, dict.fromkeys(by_id, "no_evidence"), set()
    for proposal in proposals:
        if (
            not isinstance(proposal, dict)
            or set(proposal) != _FIELDS
            or any(type(value) is not str for value in proposal.values())
            or proposal["ticket_id"] not in by_id
            or proposal["ticket_id"] in seen
            or proposal["category"] not in CATEGORIES
        ):
            raise ValueError("Неверное представление подтвержденных фрагментов")
        identity = proposal["ticket_id"]
        seen.add(identity)
        if proposal["category"] not in ACTIONABLE:
            reasons[identity] = "non_actionable_category"
            continue
        source = by_id[identity]
        if len(source["text"]) > MAX_TEXT_CHARS:
            reasons[identity] = "input_too_large"
            continue
        text = candidate_text(source["text"])
        payload = {key: value for key, value in proposal.items() if key != "ticket_id"}
        item = extract_spans(text, lambda _: payload)
        reasons[identity] = item.context.reason
        if not item.context.groupable:
            continue
        occurrences = set(_code_occurrences(text))
        codes = {code for code, _, _ in occurrences}
        if len(codes) > 1:
            reasons[identity] = "ambiguous_error_code"
            continue
        start, end = item.symptom_span
        covered = {code for code, a, b in occurrences if start <= a and b <= end}
        if covered != codes:
            reasons[identity] = "uncovered_error_code"
            continue
        operation, symptom = item.context.operation, item.context.symptom
        op_negation, symptom_negation = _negation(operation), _negation(symptom)
        if op_negation is None or symptom_negation is None:
            reasons[identity] = "ambiguous_negation"
            continue
        negation_refusal = _guard_negation(
            text, item.operation_span, item.symptom_span, symptom_negation
        )
        if negation_refusal:
            reasons[identity] = negation_refusal
            continue
        evidence[identity] = QuoteEvidence(
            identity,
            source["product"],
            item.context.category,
            operation,
            symptom,
            sha256(text.encode()),
            item.operation_span,
            item.symptom_span,
            next(iter(codes), None),
            (*op_negation, "|", *symptom_negation),
        )
    return evidence, reasons


def _pair_reason(left, right):
    if left is None or right is None:
        return "evidence_abstained"
    if left.product != right.product:
        return "different_product"
    if left.category != right.category:
        return "different_category"
    if left.error_code != right.error_code:
        return "different_error_code"
    if left.negation != right.negation:
        return "different_negation"
    return "context_pass"


def _similarity(left, right):
    return round(max(-1.0, min(1.0, cosine(left, right))), 8)


def score_pairs(ticket_ids, evidence, provider: EmbeddingProvider):
    """Вычисляет независимые сходства операции и симптома внутри допустимых пар."""
    ids = sorted(ticket_ids)
    if len(set(ids)) != len(ids) or len(ids) > MAX_TICKETS or set(evidence) - set(ids):
        raise ValueError("Некорректный объем или состав поиска")
    pairs, participants = [], set()
    for left, right in combinations(ids, 2):
        reason = _pair_reason(evidence.get(left), evidence.get(right))
        pairs.append(
            {
                "left": left,
                "right": right,
                "reason": reason,
                "operation_similarity": None,
                "symptom_similarity": None,
                "score": None,
                "normalized_match": False,
            }
        )
        if reason == "context_pass":
            participants.update((left, right))
    fragments = sorted(
        {
            fragment
            for identity in participants
            for fragment in (evidence[identity].operation, evidence[identity].symptom)
        }
    )
    vectors = []
    for start in range(0, len(fragments), BATCH_SIZE):
        vectors.extend(
            _validated_vectors(provider, fragments[start : start + BATCH_SIZE])
        )
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) > 1:
        raise ValueError("Размерность эмбеддингов меняется между пакетами")
    by_fragment = dict(zip(fragments, vectors))
    keys = {
        identity: (
            normalize_fragment(evidence[identity].operation),
            normalize_fragment(evidence[identity].symptom),
        )
        for identity in participants
    }
    for pair in pairs:
        if pair["reason"] != "context_pass":
            continue
        left, right = evidence[pair["left"]], evidence[pair["right"]]
        operation = _similarity(
            by_fragment[left.operation], by_fragment[right.operation]
        )
        symptom = _similarity(by_fragment[left.symptom], by_fragment[right.symptom])
        pair.update(
            operation_similarity=operation,
            symptom_similarity=symptom,
            score=min(operation, symptom),
            normalized_match=keys[left.ticket_id] == keys[right.ticket_id],
        )
    return pairs, {
        "embedded_fragments": len(fragments),
        "embedded_tickets": len(participants),
        "vector_dimensions": next(iter(dimensions), None),
    }


def rank_candidates(ticket_ids, pairs, method, threshold=None, *, k=TOP_K):
    """Формирует до k прямых рекомендаций на обращение."""
    if (
        method not in {"semantic", "normalized"}
        or type(k) is not int
        or not 1 <= k <= TOP_K
    ):
        raise ValueError("Некорректные параметры поиска")
    if threshold is not None and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not isfinite(threshold)
        or not 0 < threshold <= 1
    ):
        raise ValueError("Некорректный порог сходства")
    neighbors = defaultdict(list)
    for pair in pairs:
        if pair["reason"] != "context_pass":
            continue
        if method == "normalized":
            score = 1.0 if pair["normalized_match"] else None
        else:
            score = pair["score"]
            if threshold is None or score is None or score < threshold:
                continue
        if score is not None:
            left, right = pair["left"], pair["right"]
            neighbors[left].append((score, right))
            neighbors[right].append((score, left))
    return {
        identity: [
            other
            for _, other in sorted(
                neighbors[identity], key=lambda item: (-item[0], item[1])
            )[:k]
        ]
        for identity in sorted(ticket_ids)
    }
