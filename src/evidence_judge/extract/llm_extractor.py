"""LLM-backed claim extraction with deterministic post-validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from evidence_judge.core.claims import Claim, ClaimSet
from evidence_judge.core.verdict import Usage
from evidence_judge.extract.base import MAX_CLAIMS, MAX_PREDICATES_PER_CLAIM
from evidence_judge.extract.merge import cap_claims, dedupe_claims, renumber
from evidence_judge.extract.prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt
from evidence_judge.extract.schema import ClaimExtraction, ExtractedClaim
from evidence_judge.extract.text import Origin, quote_in
from evidence_judge.llm.base import LLMClient, LLMRequest, Tier

_STEP_MARKER = re.compile(r"^\s*\[step \d+\]\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractionStats:
    proposed: int
    hallucinated_quotes: int
    duplicates: int
    truncated: int

    def note(self) -> str:
        return (
            f"proposed={self.proposed}; hallucinated_quotes={self.hallucinated_quotes}; "
            f"duplicates={self.duplicates}; truncated={self.truncated}"
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
        sources: dict[Origin, str] = {"answer": answer, "reasoning": reasoning}
        grounded: list[Claim] = []
        hallucinated = 0
        for proposed in output.claims:
            claim = _ground(proposed, sources)
            if claim is None:
                hallucinated += 1
            else:
                grounded.append(claim)
        unique, duplicates = dedupe_claims(grounded, max_predicates=MAX_PREDICATES_PER_CLAIM)
        capped, truncated = cap_claims(unique, self._max_claims)
        stats = ExtractionStats(
            proposed=len(output.claims),
            hallucinated_quotes=hallucinated,
            duplicates=duplicates,
            truncated=truncated,
        )
        claim_set = ClaimSet(
            claims=renumber(capped), extractor=self.name, coverage_note=stats.note()
        )
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
