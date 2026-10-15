from __future__ import annotations

import dataclasses
import functools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from evidence_judge.checks.matching import host_allowed, url_host
from evidence_judge.core.evidence import NetworkEvent, is_web_page_url
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.verdict import PredicateResult, Usage
from evidence_judge.llm.base import LLMClient


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """Which hosts may prove a claim or an `expect` entry.

    The site is the spec's (or --site) `site` together with `allowed_hosts`; with neither, it is
    the run's primary host, inferred from the first page the run was on (`inferred`). Mutations
    and page states must be on a site host or a subdomain of it (a leading 'www.' is ignored),
    so a POST to a third party cannot prove "order placed" and neither can a confirmation page
    on another host. Other requests count on any host only while the site is inferred; once
    `site` or `allowed_hosts` names it, a request off the site proves nothing. Only http and
    https page states count: data:, about:, view-source:, file:, browser-internal and extension
    pages never do.
    """

    allowed_hosts: tuple[str, ...] = ()
    primary_host: str | None = None
    site: str | None = None

    @classmethod
    def of(
        cls, index: EvidenceIndex, allowed_hosts: Sequence[str] = (), site: str | None = None
    ) -> EvidenceScope:
        return cls(tuple(allowed_hosts), index.primary_host, site)

    @property
    def inferred(self) -> bool:
        """True when nothing but the run itself says which site it was about."""
        return not (self.site or self.allowed_hosts)

    @property
    def site_hosts(self) -> list[str]:
        if self.inferred:
            return [self.primary_host.removeprefix("www.")] if self.primary_host else []
        named = [self.site.removeprefix("www.")] if self.site else []
        return list(dict.fromkeys([*named, *self.allowed_hosts]))

    def describe(self) -> str:
        if self.inferred:
            label = "the run's host"
        else:
            label = "the allowed hosts" if self.allowed_hosts else "the site"
        return f"{label} ({', '.join(self.site_hosts)})"

    def request_allowed(self, event: NetworkEvent) -> bool:
        if not (event.is_mutation or not self.inferred):
            return True
        hosts = self.site_hosts
        return not hosts or host_allowed(url_host(event.url), hosts)

    def page_allowed(self, url: str) -> bool:
        return _page_allowed(url, tuple(self.site_hosts))


@functools.lru_cache(maxsize=4096)
def _page_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    if not is_web_page_url(url):
        return False
    return not hosts or host_allowed(url_host(url), list(hosts))


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
