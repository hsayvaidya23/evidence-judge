"""Guards on evidence that proves something: a claim or an `expect` entry.

A predicate that is supported on the run can still prove nothing about what the agent did: it
only holds on evidence from after the agent ran its own code in the page or mocked a response
(core.provenance), the run already met it before the agent acted (the baseline), or it only
holds where a page, a URL or a response echoes the agent's own input. Claims and `expect` entries share these guards;
`forbid` entries and constraints do not use them, since masking or a baseline could hide a real
violation.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import unquote_plus

from evidence_judge.checks._common import quoted, result
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.index import is_short_word
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    UrlPredicate,
    ValuePredicate,
)
from evidence_judge.core.provenance import doubt
from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.textnorm import normalize

MAX_SHOWN_INPUTS: Final = 3


def reads_agent_reachable_text(predicate: Predicate) -> bool:
    """True if `predicate` reads something the site can repeat the agent's input back in: page
    text, a URL, or a request (its URL or response); an action or a screenshot cannot."""
    return isinstance(
        predicate, PageTextPredicate | UrlPredicate | RequestPredicate | ValuePredicate
    )


def reads_short_word(predicate: Predicate) -> bool:
    """True if a single short word the agent typed (is_short_word) could be all `predicate`
    reads: it looks for a short word, a short string value, or matches a regex (which can
    capture one, or match a URL path the agent typed). Such a predicate is checked with short
    words masked too; others are not, so typing "Your" does not mask "Thank you for your order".
    """
    match predicate:
        case PageTextPredicate():
            return is_short_word(predicate.contains)
        case UrlPredicate():
            needles = [predicate.contains, predicate.query_contains]
            return predicate.regex is not None or any(n and is_short_word(n) for n in needles)
        case RequestPredicate():
            needles = [
                *predicate.url_contains,
                *predicate.path_contains,
                *predicate.query_contains,
                *predicate.response_contains,
            ]
            return predicate.url_regex is not None or any(is_short_word(n) for n in needles)
        case ValuePredicate():
            short = isinstance(predicate.value, str) and is_short_word(predicate.value)
            return short or (
                predicate.in_request is not None and reads_short_word(predicate.in_request)
            )
        case _:
            return False


async def echoed(
    predicate: Predicate, supported: PredicateResult, ctx: CheckContext, registry: CheckerRegistry
) -> str | None:
    """Why `predicate`, `supported` on the evidence, only holds on an echo of the agent's own
    input, or None.

    The predicate is evaluated again with every input (EvidenceIndex.agent_inputs) masked out of
    everything the site can echo it in (EvidenceIndex.without_agent_input): page text, URLs and
    response bodies. If it no longer holds, what supported it was the agent's input.
    """
    if not reads_agent_reachable_text(predicate):
        return None
    masked = ctx.without_agent_input(short_words=reads_short_word(predicate))
    if masked.index is ctx.index:
        return None
    if (await registry.evaluate(predicate, masked)).status is Support.SUPPORTED:
        return None
    inputs = _cited_inputs(ctx.index.agent_inputs(), supported)
    shown = ", ".join(quoted(i) for i in inputs[:MAX_SHOWN_INPUTS])
    more = f" and {len(inputs) - MAX_SHOWN_INPUTS} more" if len(inputs) > MAX_SHOWN_INPUTS else ""
    return (
        "it holds only where a page, URL or response echoes the agent's own input (text it typed, "
        f"pasted or put in a URL: {shown}{more}), so it proves nothing"
    )


def _cited_inputs(inputs: tuple[str, ...], supported: PredicateResult) -> tuple[str, ...]:
    """The inputs that appear in what `supported` cites, else all of them."""
    cited = " ".join(normalize(unquote_plus(c.excerpt or "")) for c in supported.citations)
    return tuple(i for i in inputs if i in cited) or inputs


async def tampered(
    predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry
) -> str | None:
    """Why `predicate`, supported on the evidence, only holds on evidence the agent made: after
    it ran code in the page, its route handler answered a request or it uploaded a file the
    recording does not hold (core.provenance), or None.
    The predicate is evaluated again on the evidence from before that; if it no longer holds,
    the agent's own code or responses supported it."""
    why = doubt(ctx.index.run)
    if why is None:
        return None
    if (await registry.evaluate(predicate, ctx.untampered())).status is Support.SUPPORTED:
        return None
    return f"it holds only on evidence the agent made itself ({why}), so it proves nothing"


def _anywhere(predicate: Predicate) -> Predicate:
    """`predicate` read on every page before the agent acted, not only the last one: a page the
    agent opened first and comes back to at the end was on screen before it did anything."""
    match predicate:
        case PageTextPredicate(scope=Scope.FINAL) | UrlPredicate(scope=Scope.FINAL):
            return predicate.model_copy(update={"scope": Scope.ANY})
        case ValuePredicate(scope=Scope.FINAL, page_regex=str()):
            return predicate.model_copy(update={"scope": Scope.ANY})
        case _:
            return predicate


async def unproving(
    predicate: Predicate,
    supported: PredicateResult,
    ctx: CheckContext,
    registry: CheckerRegistry,
    *,
    check_start: bool = True,
) -> str | None:
    """Why `predicate`, `supported` on the evidence, still proves nothing in this run, or None:
    a predicate that only holds on evidence the agent made (`tampered`); with `check_start`, a
    deterministic predicate the run met before the agent acted; and a
    predicate that only holds on an echo of the agent's own input."""
    if (reason := await tampered(predicate, ctx, registry)) is not None:
        return reason
    if check_start and registry.is_deterministic(predicate):
        before = await registry.evaluate(_anywhere(predicate), ctx.at_start())
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
