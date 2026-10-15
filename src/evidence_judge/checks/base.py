from __future__ import annotations

import dataclasses
import functools
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from evidence_judge.core.evidence import NetworkEvent, is_web_page_url
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.provenance import untampered
from evidence_judge.core.site import Site, on_any
from evidence_judge.core.verdict import PredicateResult, Usage
from evidence_judge.llm.base import LLMClient


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """Which sites may prove a claim or an `expect` entry.

    The sites are the spec's (or --site) `site` and its `allowed_hosts`; with neither, they are
    the site of the run's first page, host and port (`inferred`). A site covers its host with
    or without a leading 'www.', on its own port only, and no subdomain unless `allowed_hosts`
    names it (or a '*.' wildcard of it). Mutations and page states must be on a site, so a POST
    to a third party cannot prove "order placed" and neither can a confirmation page on another
    host or port. Other requests count on any host only while the site is inferred; once
    `site` or `allowed_hosts` names it, a request off the site proves nothing. Only http and
    https page states count: data:, about:, view-source:, file:, browser-internal and extension
    pages never do.
    """

    sites: tuple[Site, ...] = ()
    inferred: bool = True
    """True when nothing but the run itself says which site it was about."""

    @classmethod
    def of(
        cls, index: EvidenceIndex, allowed_hosts: Sequence[str] = (), site: str | None = None
    ) -> EvidenceScope:
        hosts = [*([site] if site else []), *allowed_hosts]
        if hosts:
            named = (Site.parse(h, wildcard=True) for h in hosts)
            return cls(tuple(dict.fromkeys(named)), inferred=False)
        primary = index.primary_site
        return cls((primary,) if primary else ())

    def describe(self) -> str:
        if self.inferred:
            label = "the run's host"
        else:
            label = "the allowed hosts" if len(self.sites) > 1 else "the site"
        return f"{label} ({', '.join(map(str, self.sites))})"

    def request_allowed(self, event: NetworkEvent) -> bool:
        if not (event.is_mutation or not self.inferred):
            return True
        return not self.sites or on_any(event.url, self.sites)

    def page_allowed(self, url: str) -> bool:
        return _page_allowed(url, self.sites)


@functools.lru_cache(maxsize=4096)
def _page_allowed(url: str, sites: tuple[Site, ...]) -> bool:
    if not is_web_page_url(url):
        return False
    return not sites or on_any(url, sites)


_UNTAMPERED: weakref.WeakKeyDictionary[EvidenceIndex, EvidenceIndex] = weakref.WeakKeyDictionary()


def _untampered_index(index: EvidenceIndex) -> EvidenceIndex:
    cached = _UNTAMPERED.get(index)
    if cached is None:
        run = untampered(index.run)
        cached = _UNTAMPERED[index] = index if run is index.run else EvidenceIndex(run)
    return cached


@dataclass(slots=True)
class CheckContext:
    index: EvidenceIndex
    llm: LLMClient | None = None
    usage: Usage = field(default_factory=Usage)
    scope: EvidenceScope | None = None
    """Set when evidence must prove something (claims, `expect`); None sees every host."""

    def scoped(self, scope: EvidenceScope | None) -> CheckContext:
        """The same context, sharing its usage, with another scope."""
        return dataclasses.replace(self, scope=scope)

    def at_start(self) -> CheckContext:
        """The same context over the evidence before the agent acted (EvidenceIndex.baseline)."""
        return dataclasses.replace(self, index=self.index.baseline, llm=None)

    def untampered(self) -> CheckContext:
        """The same context over the evidence the site alone made (core.provenance.untampered),
        without an LLM; itself when the agent injected and mocked nothing."""
        index = _untampered_index(self.index)
        return self if index is self.index else dataclasses.replace(self, index=index, llm=None)

    def without_agent_input(self, *, short_words: bool = False) -> CheckContext:
        """The same context over the evidence with the agent's own input masked out; with
        `short_words`, also inputs that are a single short word (core.index.is_short_word)."""
        index = (
            self.index.without_short_word_input if short_words else self.index.without_agent_input
        )
        return dataclasses.replace(self, index=index, llm=None)


class PredicateChecker(Protocol):
    """Evaluates one predicate kind. `deterministic` checkers never call an LLM."""

    kind: str
    deterministic: bool

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult: ...
