"""Offline semantic matching contracts; no external API calls."""
from dataclasses import dataclass
from math import sqrt
from typing import Protocol
import re

class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

@dataclass(frozen=True)
class Match:
    left: str
    right: str
    similarity: float
    accepted: bool
    reason: str

def cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        raise ValueError('Embeddings must be nonempty and have matching dimensions')
    if any(not (-1e100 < x < 1e100) for x in a + b):
        raise ValueError('Nonfinite embedding value')
    na = sqrt(sum(x*x for x in a)); nb = sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0:
        raise ValueError('Zero embedding')
    return sum(x*y for x,y in zip(a,b)) / (na*nb)

def normalize(text: str) -> str:
    return ' '.join(re.findall(r'[\w]+', text.casefold(), flags=re.UNICODE))

def match_pair(left: dict, right: dict, embeddings: tuple[list[float], list[float]], threshold: float = .86) -> Match:
    a,b = left,right
    if a['product'] != b['product']:
        return Match(a['id'],b['id'],0.0,False,'different_product')
    if a['category'] != b['category']:
        return Match(a['id'],b['id'],0.0,False,'different_category')
    if a.get('workflow') and b.get('workflow') and a['workflow'] != b['workflow']:
        return Match(a['id'],b['id'],0.0,False,'different_workflow')
    if not 0 < threshold <= 1:
        raise ValueError('Threshold out of range')
    similarity = cosine(*embeddings)
    return Match(a['id'],b['id'],similarity,similarity >= threshold,
                 'similarity_pass' if similarity >= threshold else 'below_threshold')

def evaluate_pairs(gold: list[bool], predicted: list[bool]) -> dict:
    if len(gold) != len(predicted) or not gold:
        raise ValueError('Gold and predictions must be nonempty and equal-length')
    tp=sum(g and p for g,p in zip(gold,predicted))
    fp=sum(not g and p for g,p in zip(gold,predicted))
    fn=sum(g and not p for g,p in zip(gold,predicted))
    precision=tp/(tp+fp) if tp+fp else 0.0
    recall=tp/(tp+fn) if tp+fn else 0.0
    return {'tp':tp,'fp':fp,'fn':fn,'precision':precision,'recall':recall,
            'f1':2*precision*recall/(precision+recall) if precision+recall else 0.0}
