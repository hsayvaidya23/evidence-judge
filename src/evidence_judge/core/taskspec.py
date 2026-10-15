"""Task specs: what evidence a successful run *must* contain, independent of what the agent says.

Example (YAML):

    id: buy-kettle
    task: Buy a red kettle under $30
    site: https://shop.example.com
    expect:
      - id: order_submitted
        description: An order request succeeded
        all:
          - {kind: request, method: POST, url_contains: [/api/orders]}
      - id: confirmation_shown
        any:
          - {kind: page_text, contains: Thank you for your order}
    allow:
      - {kind: request, method: POST, url_contains: [/api/cart]}   # max_count: N caps it
    forbid:
      - id: no_account_changes
        any:
          - {kind: request, method: POST, url_contains: [/api/account]}
    constraints:
      - {kind: value, field: price, json_path: items.0.price, op: "<=", value: 30}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evidence_judge.core.evidence import MUTATION_METHODS
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Pattern,
    Predicate,
    RequestPredicate,
    ValuePredicate,
)
from evidence_judge.core.site import Site

_SHA256: Final = re.compile(r"[0-9a-fA-F]{64}")


class Expectation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str | None = None
    all: list[Predicate] = Field(default_factory=list[Predicate])
    any: list[Predicate] = Field(default_factory=list[Predicate])
    max_count: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How many matching mutations the expectation explains (1 when unset; more are "
            "duplicate side effects). When set, more matching mutations also break it."
        ),
    )

    def request_predicates(self) -> list[RequestPredicate]:
        """Its request predicates, including the requests its value predicates read."""
        found: list[RequestPredicate] = []
        for predicate in [*self.all, *self.any]:
            if isinstance(predicate, RequestPredicate):
                found.append(predicate)
            elif isinstance(predicate, ValuePredicate) and predicate.in_request is not None:
                found.append(predicate.in_request)
        return found

    @model_validator(mode="after")
    def _non_empty(self) -> Expectation:
        if not self.all and not self.any:
            raise ValueError(
                f"expectation {self.id!r} needs at least one predicate in 'all' or 'any'"
            )
        return self


class AllowedRequest(RequestPredicate):
    """An `allow` entry: a request predicate, optionally capped at `max_count` matches."""

    max_count: int | None = Field(
        default=None,
        ge=1,
        description="How many matching mutations it explains; unlimited when unset",
    )


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    task: str
    expect: list[Expectation] = Field(default_factory=list[Expectation])
    forbid: list[Expectation] = Field(default_factory=list[Expectation])
    allow: list[AllowedRequest] = Field(
        default_factory=list[AllowedRequest],
        description="Intermediate mutations that are part of doing the task (not side effects)",
    )
    constraints: list[ValuePredicate] = Field(default_factory=list[ValuePredicate])
    allowed_hosts: list[str] = Field(
        default_factory=list[str],
        description=(
            "More hosts that are part of the site, as URLs or host names, with a port when it is "
            "not the default; '*.shop.example.com' names every subdomain. With it set, mutations "
            "to hosts outside the site and these count as side effects"
        ),
    )
    site: str | None = Field(
        default=None,
        description=(
            "The site the task is on, as a URL or host name, with a port when it is not the "
            "default; a whole host, never a path or a public suffix. Evidence that proves a claim or an expect entry must come from it (with "
            "or without 'www.', on its port) or the allowed hosts; without it or allowed_hosts, "
            "no run can pass"
        ),
    )

    failure_markers: list[Pattern] = Field(
        default_factory=list[Pattern],
        description=(
            "Regexes (case-insensitive, any language) that mark a page as a failure: a "
            "mutation answered with a redirect proves nothing when the page it lands on has "
            "one in its URL or text, e.g. 'Zahlung abgelehnt' or 'statut=refus'"
        ),
    )

    trusted_scripts: list[str] = Field(
        default_factory=list[str],
        description=(
            "SHA-256 hashes (hex) of injected scripts your harness runs that do not change what "
            "the page shows (core.provenance.script_hash); a run that runs one is not treated "
            "as having made its own evidence. The NEEDS_REVIEW reason and `evidence-judge "
            "inspect` print each script's hash"
        ),
    )

    @field_validator("trusted_scripts")
    @classmethod
    def _trusted_scripts(cls, value: list[str]) -> list[str]:
        for digest in value:
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"trusted_scripts: {digest!r} is not a SHA-256 hex digest")
        return [d.lower() for d in value]

    @field_validator("site")
    @classmethod
    def _site(cls, value: str | None) -> str | None:
        return None if value is None else str(Site.parse(value))

    @field_validator("allowed_hosts")
    @classmethod
    def _allowed_hosts(cls, value: list[str]) -> list[str]:
        return [str(Site.parse(h, what="allowed host", wildcard=True)) for h in value]

    def sites(self) -> list[Site]:
        """The site and the allowed hosts: the hosts the spec names, in order."""
        named = [self.site] if self.site else []
        return list(
            dict.fromkeys(Site.parse(h, wildcard=True) for h in [*named, *self.allowed_hosts])
        )

    def success_texts(self) -> list[str]:
        """The page texts its `expect` entries ask for, in order."""
        return list(
            dict.fromkeys(
                p.contains
                for e in self.expect
                for p in [*e.all, *e.any]
                if isinstance(p, PageTextPredicate)
            )
        )

    def changes_state(self) -> bool:
        """Whether an `expect` entry needs a mutation request: the task writes something."""
        return any(
            (p.method or "").upper() in MUTATION_METHODS
            for e in self.expect
            for p in e.request_predicates()
        )

    @model_validator(mode="after")
    def _specific_expectations(self) -> TaskSpec:
        for expectation in self.expect:
            for predicate in [*expectation.all, *expectation.any]:
                if isinstance(predicate, RequestPredicate) and (
                    predicate.method is None or not predicate.locates
                ):
                    raise ValueError(
                        f"expect {expectation.id!r}: a request predicate needs a method and a "
                        "url_contains, path_contains, query_contains, url_regex, body_contains "
                        "or response_contains criterion; one without them is met by any request"
                    )
        return self

    @classmethod
    def load(cls, path: str | Path) -> TaskSpec:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)
