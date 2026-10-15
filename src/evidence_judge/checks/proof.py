"""Guards on evidence that proves something: a claim or an `expect` entry.

A predicate that is supported on the run can still prove nothing about what the agent did: the
run already met it before the agent acted (the baseline), or it only holds where a page or URL
echoes the agent's own input. Claims and `expect` entries share these guards; `forbid` entries
and constraints do not use them, since masking or a baseline could hide a real violation.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import unquote_plus

from evidence_judge.checks._common import quoted, result
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    UrlPredicate,
    ValuePredicate,
)
from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.textnorm import normalize

MAX_SHOWN_INPUTS: Final = 3


def reads_page_or_url(predicate: Predicate) -> bool:
    match predicate:
        case PageTextPredicate() | UrlPredicate():
            return True
        case ValuePredicate(page_regex=str()):
            return True
        case _:
            return False


async def echoed(
    predicate: Predicate, supported: PredicateResult, ctx: CheckContext, registry: CheckerRegistry
) -> str | None:
    """Why `predicate`, `supported` on the evidence, only holds on an echo of the agent's own
    input, or None.

    The predicate is evaluated again with every input (EvidenceIndex.agent_inputs) masked out of
    page text and URLs; if it no longer holds, what supported it was the agent's input.
    """
    if not reads_page_or_url(predicate):
        return None
    masked = ctx.without_agent_input()
    if masked.index is ctx.index:
        return None
    if (await registry.evaluate(predicate, masked)).status is Support.SUPPORTED:
        return None
    inputs = _cited_inputs(ctx.index.agent_inputs(), supported)
    shown = ", ".join(quoted(i) for i in inputs[:MAX_SHOWN_INPUTS])
    more = f" and {len(inputs) - MAX_SHOWN_INPUTS} more" if len(inputs) > MAX_SHOWN_INPUTS else ""
    return (
        "it holds only where the page or URL echoes the agent's own input (text it typed or "
        f"put in a URL: {shown}{more}), so it proves nothing"
    )


def _cited_inputs(inputs: tuple[str, ...], supported: PredicateResult) -> tuple[str, ...]:
    """The inputs that appear in what `supported` cites, else all of them."""
    cited = " ".join(normalize(unquote_plus(c.excerpt or "")) for c in supported.citations)
    return tuple(i for i in inputs if i in cited) or inputs


async def unproving(
    predicate: Predicate,
    supported: PredicateResult,
    ctx: CheckContext,
    registry: CheckerRegistry,
    *,
    check_start: bool = True,
) -> str | None:
    """Why `predicate`, `supported` on the evidence, still proves nothing in this run, or None:
    with `check_start`, a deterministic predicate the run met before the agent acted; and a
    predicate that only holds on an echo of the agent's own input."""
    if check_start and registry.is_deterministic(predicate):
        before = await registry.evaluate(predicate, ctx.at_start())
        if before.status is Support.SUPPORTED:
            return (
                "it was already true before the agent acted, so every run of the task meets it "
                f"({before.reason})"
            )
    return await echoed(predicate, supported, ctx, registry)


async def guarded(
    predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry, what: str
) -> PredicateResult:
    """`predicate` evaluated on the evidence; a support that proves nothing (`unproving`) becomes
    NO_EVIDENCE with the reason, keeping its citations. `what` names what it was to prove."""
    res = await registry.evaluate(predicate, ctx)
    if res.status is not Support.SUPPORTED:
        return res
    reason = await unproving(predicate, res, ctx, registry)
    if reason is None:
        return res
    return result(
        Support.NO_EVIDENCE, f"Predicate cannot prove {what}: {reason}", res.checker, res.citations
    )
