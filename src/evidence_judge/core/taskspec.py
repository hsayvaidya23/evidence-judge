"""Task specs: what evidence a successful run *must* contain, independent of what the agent says.

Example (YAML):

    id: buy-kettle
    task: Buy a red kettle under $30
    expect:
      - id: order_submitted
        description: An order request succeeded
        all:
          - {kind: request, method: POST, url_contains: [/api/orders], status: 2xx}
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

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evidence_judge.core.predicates import Predicate, RequestPredicate, ValuePredicate


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
        default_factory=list[str], description="Mutations to other hosts count as side effects"
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
