"""Combining claim sets and measuring how much of the agent's narrative they cover."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher

from evidence_judge.core.claims import Claim, ClaimSet
from evidence_judge.core.predicates import Predicate
from evidence_judge.extract.base import MAX_CLAIMS
from evidence_judge.extract.text import is_claim_like, normalize, sentences

_NEAR_DUPLICATE_RATIO = 0.9
_COVERAGE_WORD_OVERLAP = 0.6
_MERGED_MAX_PREDICATES = 6


def _same_claim(a: Claim, b: Claim) -> bool:
    if a.kind != b.kind:
        return False
    text_a, text_b = normalize(a.text), normalize(b.text)
    if text_a == text_b:
        return True
    if normalize(a.quote) == normalize(b.quote) and _predicate_keys(a) == _predicate_keys(b):
        return True
    return SequenceMatcher(None, text_a, text_b).ratio() >= _NEAR_DUPLICATE_RATIO


def _predicate_keys(claim: Claim) -> frozenset[str]:
    return frozenset(p.model_dump_json() for p in claim.predicates)


def _union_predicates(
    first: Sequence[Predicate], second: Sequence[Predicate], limit: int
) -> list[Predicate]:
    seen: set[str] = set()
    out: list[Predicate] = []
    for predicate in (*first, *second):
        key = predicate.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(predicate)
    return out[:limit]


def dedupe_claims(
    claims: Iterable[Claim], *, max_predicates: int = _MERGED_MAX_PREDICATES
) -> tuple[list[Claim], int]:
    """Collapse near-identical claims, keeping the first and merging `key` and predicates."""
    kept: list[Claim] = []
    dropped = 0
    for claim in claims:
        for index, existing in enumerate(kept):
            if _same_claim(existing, claim):
                kept[index] = existing.model_copy(
                    update={
                        "key": existing.key or claim.key,
                        "predicates": _union_predicates(
                            existing.predicates, claim.predicates, max_predicates
                        ),
                    }
                )
                dropped += 1
                break
        else:
            kept.append(claim)
    return kept, dropped


def cap_claims(claims: list[Claim], limit: int = MAX_CLAIMS) -> tuple[list[Claim], int]:
    """Keep at most `limit` claims, preferring key claims, preserving original order."""
    if len(claims) <= limit:
        return claims, 0
    ranked = sorted(range(len(claims)), key=lambda i: (not claims[i].key, i))
    chosen = sorted(ranked[:limit])
    return [claims[i] for i in chosen], len(claims) - limit


def renumber(claims: Iterable[Claim]) -> list[Claim]:
    return [claim.model_copy(update={"id": f"c{n}"}) for n, claim in enumerate(claims, start=1)]


def merge_claim_sets(*sets: ClaimSet) -> ClaimSet:
    """Union of several claim sets with duplicates collapsed and ids reassigned c1..cn."""
    merged, _ = dedupe_claims(claim for claim_set in sets for claim in claim_set.claims)
    capped, _ = cap_claims(merged)
    extractors = list(dict.fromkeys(s.extractor for s in sets))
    notes = [s.coverage_note for s in sets if s.coverage_note]
    return ClaimSet(
        claims=renumber(capped),
        extractor="+".join(extractors) if extractors else "none",
        coverage_note=" | ".join(notes) if notes else None,
    )


def _covers(quote: str, sentence: str) -> bool:
    if not quote or not sentence:
        return False
    if quote in sentence or sentence in quote:
        return True
    sentence_words = set(sentence.split())
    quote_words = set(quote.split())
    return len(sentence_words & quote_words) / len(sentence_words) >= _COVERAGE_WORD_OVERLAP


def coverage(claims: Sequence[Claim], answer: str | None, reasoning: list[str]) -> float:
    """Share of claim-like sentences (asserting a completed action) backed by an extracted quote.

    Returns 1.0 when the agent asserted nothing checkable. Low values mean the extractor missed
    assertions, so the verdict should be routed to review rather than trusted.
    """
    claim_like = [normalize(s.text) for s in sentences(answer, reasoning) if is_claim_like(s.text)]
    if not claim_like:
        return 1.0
    quotes = [normalize(c.quote).strip(" .\"'") for c in claims]
    covered = sum(1 for sentence in claim_like if any(_covers(q, sentence) for q in quotes))
    return covered / len(claim_like)
