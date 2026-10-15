"""Guards on evidence that proves something: a claim or an `expect` entry.

A predicate that is supported on the run can still prove nothing about what the agent did: it
only holds on evidence from after the agent ran its own code in the page or mocked a response
(core.provenance), the run already met it before the agent acted (the baseline), it only
holds where a page, a URL or a response echoes the agent's own input, it only holds on what a
HAR merged into the trace recorded and the trace did not (`har_sourced`), or it is a screenshot
in a run where the agent typed or injected anything and nothing deterministic backs it up (an
image shows the typed field and the site's echo of it, and no mask can be applied to pixels).
Claims and `expect` entries share these guards; constraints share `har_sourced` only.
`forbid` entries and constraint violations read the evidence as recorded, since masking, a
baseline or dropping the HAR could hide a real violation.
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
    ScreenshotPredicate,
    UrlPredicate,
    ValuePredicate,
)
from evidence_judge.core.provenance import doubt, injections
from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.textnorm import normalize

MAX_SHOWN_INPUTS: Final = 3


def reads_agent_reachable_text(predicate: Predicate) -> bool:
    """True if `predicate` reads text the agent's input can be masked out of: page text, a URL,
    or a request (its URL or response). An action reads no site text. A screenshot shows the
    agent's input too, but pixels cannot be masked; `unwitnessed_screenshot` guards it instead."""
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
            exact = predicate.regex is not None or predicate.path is not None
            return exact or any(n and is_short_word(n) for n in needles)
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
    response bodies; and again with every occurrence of the text it looks for that the inputs
    make word by word masked (`covered_text`). If it no longer holds, what supported it was the
    agent's input.
    """
    if not reads_agent_reachable_text(predicate):
        return None
    if await _holds_without_input(predicate, ctx, registry):
        return None
    inputs = _cited_inputs(ctx.index.agent_inputs(), supported)
    shown = ", ".join(quoted(i) for i in inputs[:MAX_SHOWN_INPUTS])
    more = f" and {len(inputs) - MAX_SHOWN_INPUTS} more" if len(inputs) > MAX_SHOWN_INPUTS else ""
    return (
        "it holds only where a page, URL or response echoes the agent's own input (text it typed, "
        f"pasted or put in a URL: {shown}{more}), so it proves nothing"
    )


async def _holds_without_input(
    predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry
) -> bool:
    """`predicate` still holds with the agent's inputs masked out, and with every occurrence of
    the text it looks for that the inputs make word by word masked (`covered_text`)."""
    masked = ctx.without_agent_input(short_words=reads_short_word(predicate))
    if masked.index is not ctx.index and not await _supported(predicate, masked, registry):
        return False
    covered = ctx.without_covered(covered_text(predicate))
    return covered is ctx or await _supported(predicate, covered, registry)


async def _supported(predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry) -> bool:
    return (await registry.evaluate(predicate, ctx)).status is Support.SUPPORTED


def covered_text(predicate: Predicate) -> tuple[str, ...]:
    """The literal texts `predicate` looks for in page text or a response body: masked where the
    agent's inputs make them word by word (EvidenceIndex.without_covered), since several inputs
    too short to be masked alone ("In", "stock") can make a multi-word text between them."""
    match predicate:
        case PageTextPredicate():
            return (predicate.contains,)
        case RequestPredicate():
            return tuple(predicate.response_contains)
        case ValuePredicate(in_request=RequestPredicate() as request):
            return tuple(request.response_contains)
        case _:
            return ()


def unwitnessed_screenshot(predicate: Predicate, ctx: CheckContext) -> str | None:
    """Why `predicate`, a screenshot, cannot prove anything alone in this run, or None.

    A screenshot shows the field the agent typed into and any page that repeats its input back,
    and the echo masking (`echoed`) only works on text. So in a run with any agent input
    (EvidenceIndex.agent_inputs) or any injected code (core.provenance.injections), a vision
    model's "yes" may be reading the agent's own text; it proves something only with a
    deterministic co-witness, which the caller checks.
    """
    if not isinstance(predicate, ScreenshotPredicate):
        return None
    inputs = ctx.index.agent_inputs()
    if inputs:
        shown = ", ".join(quoted(i) for i in inputs[:MAX_SHOWN_INPUTS])
        more = (
            f" and {len(inputs) - MAX_SHOWN_INPUTS} more" if len(inputs) > MAX_SHOWN_INPUTS else ""
        )
        what = f"the agent put its own text into the run ({shown}{more})"
    elif injections(ctx.index.run):
        what = "the agent ran its own code in the page"
    else:
        return None
    return (
        f"{what}, and a screenshot shows it and any echo of it without a way to mask them, so a "
        "screenshot alone proves nothing; a page_text, url or request predicate that also holds "
        "is needed"
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


HAR_SOURCED: Final = (
    "only the HAR recorded it; the trace never saw this request or body, and a HAR is a file "
    "anyone can write, so it proves nothing"
)


async def har_sourced(
    predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry
) -> str | None:
    """Why `predicate`, supported on the evidence, only holds on what a HAR merged into the
    trace contributed: a request the trace never saw, or a body, status or header the trace
    recorded empty (core.evidence.trace_witnessed), or None.
    A deterministic predicate is evaluated again on what the trace alone recorded; if it no
    longer holds, the HAR's word was all that supported it. A model-judged predicate reads no
    network and is left alone."""
    traced = ctx.traced()
    if traced is ctx or not registry.is_deterministic(predicate):
        return None
    if (await registry.evaluate(predicate, traced)).status is Support.SUPPORTED:
        return None
    return HAR_SOURCED


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
    co_witnessed: bool = False,
) -> str | None:
    """Why `predicate`, `supported` on the evidence, still proves nothing in this run, or None:
    a predicate only a merged HAR's contribution supports (`har_sourced`); a predicate that only
    holds on evidence the agent made (`tampered`); with `check_start`, a deterministic predicate
    the run met before the agent acted; a predicate that only holds on an echo of the agent's
    own input; and, unless `co_witnessed` (a deterministic predicate of the same claim or
    `expect` entry is supported), a screenshot in a run with agent input
    (`unwitnessed_screenshot`)."""
    if (reason := await har_sourced(predicate, ctx, registry)) is not None:
        return reason
    if (reason := await tampered(predicate, ctx, registry)) is not None:
        return reason
    if check_start and registry.is_deterministic(predicate):
        before = await registry.evaluate(_anywhere(predicate), ctx.at_start())
        if before.status is Support.SUPPORTED:
            return (
                "it was already true before the agent acted, so every run of the task meets it "
                f"({before.reason})"
            )
    if not co_witnessed and (reason := unwitnessed_screenshot(predicate, ctx)) is not None:
        return reason
    return await echoed(predicate, supported, ctx, registry)


async def guarded(
    predicate: Predicate,
    ctx: CheckContext,
    registry: CheckerRegistry,
    what: str,
    *,
    co_witnessed: bool = False,
) -> PredicateResult:
    """`predicate` evaluated on the evidence; a support that proves nothing (`unproving`) becomes
    NO_EVIDENCE with the reason, keeping its citations. `what` names what it was to prove;
    `co_witnessed` as for `unproving`."""
    res = await registry.evaluate(predicate, ctx)
    if res.status is not Support.SUPPORTED:
        return res
    reason = await unproving(predicate, res, ctx, registry, co_witnessed=co_witnessed)
    if reason is None:
        return res
    return result(
        Support.NO_EVIDENCE, f"Predicate cannot prove {what}: {reason}", res.checker, res.citations
    )
