"""Verifiable predicates over a Run.

Claims and task expectations are compiled into predicates. An LLM may *propose* a predicate,
but only deterministic code (or, for ScreenshotPredicate, a vision check) *evaluates* it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Scope(StrEnum):
    FINAL = "final"
    ANY = "any"


class _Pred(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RequestPredicate(_Pred):
    """A network request matching these constraints was observed."""

    kind: Literal["request"] = "request"
    method: str | None = Field(default=None, description="HTTP method, e.g. POST")
    url_contains: list[str] = Field(
        default_factory=list[str],
        description="All substrings must appear in the scheme, host and path (not the query)",
    )
    path_contains: list[str] = Field(
        default_factory=list[str], description="All substrings must appear in the URL path"
    )
    query_contains: list[str] = Field(
        default_factory=list[str],
        description="All substrings must appear in the decoded query string",
    )
    url_regex: str | None = Field(default=None, description="Matched against the full URL")
    status: str | None = Field(
        default="2xx", description="Exact code ('201') or class ('2xx'); null = any"
    )
    body_contains: list[str] = Field(
        default_factory=list[str],
        description="All substrings must appear in the request body (case-insensitive)",
    )
    response_contains: list[str] = Field(default_factory=list[str])


class PageTextPredicate(_Pred):
    """Visible page text contains a phrase."""

    kind: Literal["page_text"] = "page_text"
    contains: str
    scope: Scope = Scope.FINAL


class UrlPredicate(_Pred):
    """The page URL matched."""

    kind: Literal["url"] = "url"
    contains: str | None = None
    regex: str | None = None
    scope: Scope = Scope.FINAL


class ActionPredicate(_Pred):
    """The agent performed a browser action."""

    kind: Literal["action"] = "action"
    name: str | None = Field(default=None, description="click, fill, goto, press, select, ...")
    target_contains: str | None = Field(
        default=None, description="Substring of the selector or target"
    )
    value_contains: str | None = None


class ValuePredicate(_Pred):
    """A value observed in evidence satisfies a comparison (e.g. price <= 30)."""

    kind: Literal["value"] = "value"
    field: str = Field(description="Human name of the value, e.g. 'price'")
    json_path: str | None = Field(
        default=None,
        description="Dotted path into a JSON request/response body, e.g. items.0.price",
    )
    in_request: RequestPredicate | None = Field(
        default=None, description="Which request carries the value; defaults to any mutation"
    )
    page_regex: str | None = Field(
        default=None, description="Regex with one group capturing the value from page text"
    )
    op: Literal["==", "!=", "<", "<=", ">", ">=", "contains"] = "=="
    value: str | float


class ScreenshotPredicate(_Pred):
    """A yes/no question answered by a vision model on the nearest screenshot. Last resort."""

    kind: Literal["screenshot"] = "screenshot"
    question: str
    scope: Scope = Scope.FINAL


Predicate = Annotated[
    RequestPredicate
    | PageTextPredicate
    | UrlPredicate
    | ActionPredicate
    | ValuePredicate
    | ScreenshotPredicate,
    Field(discriminator="kind"),
]
