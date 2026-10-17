"""LLM-backed claim extraction with deterministic post-validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from evidence_judge.checks.matching import contains, parse_number
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import MUTATION_METHODS
from evidence_judge.core.predicates import Predicate, RequestPredicate, ValuePredicate
from evidence_judge.core.verdict import Usage
from evidence_judge.extract.base import MAX_CLAIMS, MAX_PREDICATES_PER_CLAIM
from evidence_judge.extract.heuristic import (
    ORDER_URL,
    ORDER_WORDS,
    answer_priced,
    listed_amount,
    order_results,
    priced_amounts,
)
from evidence_judge.extract.merge import cap_claims, dedupe_claims, renumber
from evidence_judge.extract.prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt
from evidence_judge.extract.schema import ClaimExtraction, ExtractedClaim
from evidence_judge.extract.text import (
    Origin,
    Sentence,
    names_purchase,
    quote_in,
    sentences,
    split_sentences,
)
from evidence_judge.llm.base import LLMClient, LLMRequest, Tier

_STEP_MARKER = re.compile(r"^\s*\[step \d+\]\s*", re.IGNORECASE)
_ORDER_PATH = re.compile(ORDER_WORDS, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractionStats:
    proposed: int
    hallucinated_quotes: int
    duplicates: int
    truncated: int
    vacuous_predicates: int = 0

    def note(self) -> str:
        return (
            f"proposed={self.proposed}; hallucinated_quotes={self.hallucinated_quotes}; "
            f"duplicates={self.duplicates}; truncated={self.truncated}; "
            f"vacuous_predicates={self.vacuous_predicates}"
        )


class LLMClaimExtractor:
    """Asks the LLM to extract claims, then keeps only those whose quote really occurs in the
    agent output. LLM errors (including BudgetExceededError) propagate so the caller can fall
    back to the heuristic extractor.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        tier: Tier = Tier.FAST,
        max_output_tokens: int = 8_192,
        max_claims: int = MAX_CLAIMS,
    ) -> None:
        self._llm = llm
        self._tier = tier
        self._max_output_tokens = max_output_tokens
        self._max_claims = max_claims
        self.name = f"llm:{llm.name}"

    async def extract(
        self, *, task: str | None, answer: str | None, reasoning: list[str]
    ) -> tuple[ClaimSet, Usage]:
        steps = [step for step in reasoning if step.strip()]
        if not (answer and answer.strip()) and not steps:
            return ClaimSet(extractor=self.name, coverage_note="no agent output"), Usage()
        request = LLMRequest(
            system=EXTRACTION_SYSTEM_PROMPT,
            prompt=build_extraction_prompt(task=task, answer=answer, reasoning=steps),
            tier=self._tier,
            max_output_tokens=self._max_output_tokens,
        )
        output, usage = await self._llm.generate(request, ClaimExtraction)
        claim_set, _ = self.postprocess(output, answer=answer or "", reasoning="\n".join(steps))
        return claim_set, usage

    def postprocess(
        self, output: ClaimExtraction, *, answer: str, reasoning: str
    ) -> tuple[ClaimSet, ExtractionStats]:
        return ground_extraction(
            output,
            answer=answer,
            reasoning=reasoning,
            extractor=self.name,
            max_claims=self._max_claims,
        )


def ground_extraction(
    output: ClaimExtraction,
    *,
    answer: str,
    reasoning: str,
    extractor: str,
    max_claims: int = MAX_CLAIMS,
) -> tuple[ClaimSet, ExtractionStats]:
    """Keep proposed claims whose quote occurs in the agent output, deduplicated and capped.

    Predicates were already filtered when `output` was validated: malformed or vacuous ones are
    dropped and counted (ExtractedClaim.dropped_predicates). A claim about what a purchase cost
    is proven only by what the order was answered with (`_order_scoped`).
    """
    sources: dict[Origin, str] = {"answer": answer, "reasoning": reasoning}
    told = sentences(answer, reasoning.split("\n"))
    bought = any(names_purchase(s.text) for s in told)
    grounded: list[Claim] = []
    hallucinated = 0
    for proposed in output.claims:
        claim = _ground(proposed, sources)
        if claim is None:
            hallucinated += 1
        else:
            grounded.append(_order_scoped(claim, told, bought))
    unique, duplicates = dedupe_claims(grounded, max_predicates=MAX_PREDICATES_PER_CLAIM)
    capped, truncated = cap_claims(unique, max_claims)
    stats = ExtractionStats(
        proposed=len(output.claims),
        hallucinated_quotes=hallucinated,
        duplicates=duplicates,
        truncated=truncated,
        vacuous_predicates=sum(c.dropped_predicates for c in output.claims),
    )
    claim_set = ClaimSet(claims=renumber(capped), extractor=extractor, coverage_note=stats.note())
    return claim_set, stats


def _ground(proposed: ExtractedClaim, sources: dict[Origin, str]) -> Claim | None:
    quote = _STEP_MARKER.sub("", proposed.quote).strip()
    other: Origin = "reasoning" if proposed.origin == "answer" else "answer"
    for origin in (proposed.origin, other):
        if quote_in(quote, sources[origin]):
            return Claim(
                id="pending",
                text=proposed.text.strip(),
                quote=quote,
                kind=proposed.kind,
                origin=origin,
                key=proposed.key,
                predicates=proposed.predicates[:MAX_PREDICATES_PER_CLAIM],
            )
    return None


def order_scoped(claims: ClaimSet, answer: str | None, reasoning: list[str]) -> ClaimSet:
    """`claims` of a run whose evidence shows an order placed, with every amount stated as a
    price proven only by the order (`_order_scoped`), however the answer words the purchase:
    "Checkout complete. It cost $19.00." names no purchase, but the order it placed is what the
    amount is checked against, not any page that lists it."""
    told = sentences(answer, reasoning)
    scoped = [_order_scoped(claim, told, True) for claim in claims.claims]
    return claims.model_copy(update={"claims": scoped})


def _order_scoped(claim: Claim, told: list[Sentence], bought: bool) -> Claim:
    """`claim` proven only by the order, when it is about what a purchase cost.

    An amount the claim's sentence states as a price (heuristic.priced_amounts), in a run that
    asserts a purchase, is what the purchase cost: any page of the run could list it for another
    item. When a predicate checks such an amount, or a value claim quotes one, only a mutation
    request to an order's URL (`url_regex` heuristic.ORDER_URL, or a `path_contains` that names
    an order, checkout, payment or booking) whose answer shows it (`result_contains` or
    `response_contains`) is kept: a `POST /api/cart` answered with the price proves nothing
    about the order. Without one the claim gets the heuristic's predicate
    (heuristic.order_results).
    """
    paid = [a for a in _paid_amounts(claim, told, bought) if _about(claim, a)]
    if not paid:
        return claim
    kept = [p for p in claim.predicates if _answered_with(p, paid)]
    return claim.model_copy(update={"predicates": kept or order_results(paid)})


def _paid_amounts(claim: Claim, told: list[Sentence], bought: bool) -> list[str]:
    said = [s for s in told if s.origin == claim.origin and quote_in(claim.quote, s.text)]
    said = said or [Sentence(t, claim.origin) for t in split_sentences(claim.quote)]
    priced = answer_priced(told)
    amounts = [
        match.group(0).strip()
        for sentence in said
        if bought or names_purchase(sentence.text)
        for match, _ in priced_amounts(sentence, purchase=True)
        if not listed_amount(sentence, match, priced)
    ]
    return list(dict.fromkeys(amounts))


def _about(claim: Claim, amount: str) -> bool:
    if claim.kind is ClaimKind.VALUE and contains(claim.quote, amount):
        return True
    return any(_checks(p, amount) for p in claim.predicates)


def _checks(predicate: Predicate, amount: str) -> bool:
    if isinstance(predicate, ValuePredicate):
        wanted = parse_number(amount)
        return wanted is not None and parse_number(predicate.value) == wanted
    return contains(predicate.model_dump_json(), amount)


def _answered_with(predicate: Predicate, amounts: list[str]) -> bool:
    if not isinstance(predicate, RequestPredicate):
        return False
    if (predicate.method or "").upper() not in MUTATION_METHODS:
        return False
    if predicate.url_regex != ORDER_URL and not any(
        _ORDER_PATH.search(p) for p in predicate.path_contains
    ):
        return False
    answered = [*predicate.result_contains, *predicate.response_contains]
    return all(any(contains(text, a) for text in answered) for a in amounts)
