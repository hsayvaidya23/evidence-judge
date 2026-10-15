from __future__ import annotations

import dataclasses
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

    With `allowed_hosts` set, only requests and page states on those hosts (or their subdomains)
    count. Without it, mutations and page states must be on the run's primary host or a
    subdomain of it (a leading 'www.' is ignored), so a POST to a third party cannot prove
    "order placed" and neither can a confirmation page on another host; other reads on any host
    still count. Only http and https page states count: data:, about:, view-source:, file:,
    browser-internal and extension pages never do.
    """

    allowed_hosts: tuple[str, ...] = ()
    primary_host: str | None = None

    @classmethod
    def of(cls, index: EvidenceIndex, allowed_hosts: Sequence[str] = ()) -> EvidenceScope:
        return cls(tuple(allowed_hosts), index.primary_host)

    @property
    def site_hosts(self) -> list[str]:
        if self.allowed_hosts:
            return list(self.allowed_hosts)
        return [self.primary_host.removeprefix("www.")] if self.primary_host else []

    def describe(self) -> str:
        label = "the allowed hosts" if self.allowed_hosts else "the run's host"
        return f"{label} ({', '.join(self.site_hosts)})"

    def request_allowed(self, event: NetworkEvent) -> bool:
        if not (event.is_mutation or self.allowed_hosts):
            return True
        hosts = self.site_hosts
        return not hosts or host_allowed(url_host(event.url), hosts)

    def page_allowed(self, url: str) -> bool:
        if not is_web_page_url(url):
            return False
        hosts = self.site_hosts
        return not hosts or host_allowed(url_host(url), hosts)


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

    def without_agent_input(self) -> CheckContext:
        """The same context over the evidence with the agent's own input masked out."""
        return dataclasses.replace(self, index=self.index.without_agent_input, llm=None)


class PredicateChecker(Protocol):
    """Evaluates one predicate kind. `deterministic` checkers never call an LLM."""

    kind: str
    deterministic: bool

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult: ...
