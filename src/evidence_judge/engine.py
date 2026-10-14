"""The judging pipeline: claims → predicates → evidence checks → expectations → dimensions → outcome."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from evidence_judge import __version__
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.dimensions import assess_dimensions
from evidence_judge.checks.expectations import evaluate_spec
from evidence_judge.checks.outcome import decide
from evidence_judge.checks.registry import CheckerRegistry, default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Channel, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import ClaimVerdict, RunVerdict, Usage
from evidence_judge.extract import ClaimExtractor, HeuristicExtractor, LLMClaimExtractor, coverage
from evidence_judge.llm.base import (
    BudgetExceededError,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMRequest,
    T,
)
from evidence_judge.llm.budget import Budget, run_budget

log = logging.getLogger(__name__)

_HALLUCINATED = re.compile(r"hallucinated_quotes=(\d+)")
_CORE_CHANNELS = (Channel.NETWORK, Channel.SNAPSHOTS, Channel.FINAL_STATE)


@dataclass(slots=True)
class JudgeOptions:
    offline: bool = False
    spec: TaskSpec | None = None
    claims: ClaimSet | None = None


@dataclass(slots=True)
class _Extraction:
    claims: ClaimSet
    usage: Usage = field(default_factory=Usage)
    warnings: list[str] = field(default_factory=list[str])
    fallback: bool = False


class _FatalErrorTrap:
    """Remembers an `LLMConfigError` a check swallowed, so the run fails instead of degrading."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.name = inner.name
        self.error: LLMConfigError | None = None

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        try:
            return await self.inner.generate(request, schema)
        except LLMConfigError as exc:
            if self.error is None:
                self.error = exc
            raise


class Judge:
    """Grades one run. Stateless between runs; safe to reuse."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient | None = None,
        *,
        registry: CheckerRegistry | None = None,
        extractor: ClaimExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._registry = registry or default_registry(
            screenshots_enabled=settings.screenshots_to_llm
        )
        self._extractor = extractor

    async def judge(self, run: Run, options: JudgeOptions | None = None) -> RunVerdict:
        """Judge one run. Every LLM call made for it shares one fresh `settings.max_cost_usd`
        budget, so a Judge reused across runs, or judging runs concurrently, caps each run."""
        with run_budget(Budget(self._settings.max_cost_usd)):
            return await self._judge(run, options or JudgeOptions())

    async def _judge(self, run: Run, opts: JudgeOptions) -> RunVerdict:
        llm = None if opts.offline or self._llm is None else _FatalErrorTrap(self._llm)
        task = run.task or (opts.spec.task if opts.spec else None)

        extraction = await self._extract(run, task, llm, opts)
        ctx = CheckContext(index=EvidenceIndex(run), llm=llm)
        ctx.usage.add(extraction.usage)

        claim_verdicts = list(
            await asyncio.gather(
                *(judge_claim(c, ctx, self._registry) for c in extraction.claims.claims)
            )
        )
        expectations = await evaluate_spec(opts.spec, ctx, self._registry) if opts.spec else []
        if llm is not None and llm.error is not None:
            raise llm.error
        dimensions = assess_dimensions(run, ctx.index, claim_verdicts, expectations, opts.spec)
        claim_coverage = coverage(extraction.claims.claims, run.agent_answer, [])
        outcome, score, summary = decide(
            claim_verdicts,
            expectations,
            dimensions,
            coverage=claim_coverage,
            review_threshold=self._settings.review_coverage_threshold,
            extraction_fallback=extraction.fallback,
            has_spec=opts.spec is not None,
        )
        return RunVerdict(
            judge_version=__version__,
            run_id=run.run_id,
            task=task,
            outcome=outcome,
            score=score,
            summary=summary,
            claims=claim_verdicts,
            expectations=expectations,
            dimensions=dimensions,
            claim_coverage=claim_coverage,
            deterministic_share=self._deterministic_share(claim_verdicts),
            warnings=[*extraction.warnings, *self._capture_warnings(run)],
            usage=ctx.usage,
        )

    async def _extract(
        self, run: Run, task: str | None, llm: LLMClient | None, opts: JudgeOptions
    ) -> _Extraction:
        if opts.claims is not None:
            return _Extraction(claims=opts.claims)
        extractor = self._extractor or (LLMClaimExtractor(llm) if llm else HeuristicExtractor())
        try:
            claims, usage = await extractor.extract(
                task=task, answer=run.agent_answer, reasoning=run.agent_reasoning
            )
        except (LLMError, BudgetExceededError) as exc:
            if isinstance(extractor, HeuristicExtractor) or isinstance(exc, LLMConfigError):
                raise
            log.warning("LLM claim extraction failed, falling back to heuristics: %s", exc)
            claims, usage = await HeuristicExtractor().extract(
                task=task, answer=run.agent_answer, reasoning=run.agent_reasoning
            )
            return _Extraction(
                claims=claims,
                usage=usage,
                warnings=[f"LLM claim extraction failed ({exc}); used heuristic extraction"],
                fallback=True,
            )
        dropped = _HALLUCINATED.search(claims.coverage_note or "")
        warnings = (
            [
                f"claim extraction dropped {dropped.group(1)} claim(s) whose quotes were not in the agent output"
            ]
            if dropped and int(dropped.group(1)) > 0
            else []
        )
        return _Extraction(claims=claims, usage=usage, warnings=warnings)

    def _deterministic_share(self, verdicts: Sequence[ClaimVerdict]) -> float | None:
        decided = [v for v in verdicts if v.checker != "none"]
        if not decided:
            return None
        model_kinds = self._registry.model_kinds()
        deterministic = [v for v in decided if not set(v.checker.split("+")) & model_kinds]
        return len(deterministic) / len(decided)

    @staticmethod
    def _capture_warnings(run: Run) -> list[str]:
        missing = [c.value for c in _CORE_CHANNELS if c not in run.captured]
        if not missing:
            return []
        return [
            f"evidence not captured: {', '.join(missing)}; claims needing it cannot be "
            "contradicted, only left unverified"
        ]
