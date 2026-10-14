"""Verifiable predicates over a Run.

Claims and task expectations are compiled into predicates. An LLM may *propose* a predicate,
but only deterministic code (or, for ScreenshotPredicate, a vision check) *evaluates* it.

Vacuous predicates are rejected when they are built: a predicate that every run satisfies
(an empty or one-character substring, page text of a single digit or punctuation, a request or
action with no criteria) would let a fabricated claim pass. Strings are stripped; substrings
need at least MIN_SUBSTRING characters and page text at least MIN_PAGE_TEXT, with a letter or
digit. `vacuity_reason` adds the stricter bar a predicate must meet to *prove a claim*.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

MIN_SUBSTRING: Final = 2
MIN_PAGE_TEXT: Final = 3
_WORD = re.compile(r"[^\W_]+")
_DIGIT = re.compile(r"\d")


def _non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty or whitespace")
    return stripped


def _substring(value: str) -> str:
    stripped = _non_blank(value)
    if len(stripped) < MIN_SUBSTRING:
        raise ValueError(
            f"substring {stripped!r} is shorter than {MIN_SUBSTRING} characters and matches "
            "almost anything"
        )
    return stripped


def _page_text(value: str) -> str:
    stripped = _non_blank(value)
    if len(stripped) < MIN_PAGE_TEXT or not _WORD.search(stripped):
        raise ValueError(
            f"page text {stripped!r} needs at least {MIN_PAGE_TEXT} characters including a "
            "letter or digit; shorter text matches almost any page"
        )
    return stripped


def _pattern(value: str) -> str:
    if not value.strip():
        raise ValueError("regex must not be empty")
    return value


NonBlank = Annotated[str, AfterValidator(_non_blank)]
Substring = Annotated[str, AfterValidator(_substring)]
PageText = Annotated[str, AfterValidator(_page_text)]
Pattern = Annotated[str, AfterValidator(_pattern)]


class Scope(StrEnum):
    FINAL = "final"
    ANY = "any"


class _Pred(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RequestPredicate(_Pred):
    """A network request matching these constraints was observed."""

    kind: Literal["request"] = "request"
    method: NonBlank | None = Field(default=None, description="HTTP method, e.g. POST")
    url_contains: list[Substring] = Field(
        default_factory=list[Substring],
        description="All substrings must appear in the scheme, host and path (not the query)",
    )
    path_contains: list[Substring] = Field(
        default_factory=list[Substring], description="All substrings must appear in the URL path"
    )
    query_contains: list[Substring] = Field(
        default_factory=list[Substring],
        description="All substrings must appear in the decoded query string",
    )
    url_regex: Pattern | None = Field(default=None, description="Matched against the full URL")
    status: str | None = Field(
        default="2xx", description="Exact code ('201') or class ('2xx'); null = any"
    )
    body_contains: list[Substring] = Field(
        default_factory=list[Substring],
        description="All substrings must appear in the request body (case-insensitive)",
    )
    response_contains: list[Substring] = Field(default_factory=list[Substring])

    @property
    def locates(self) -> bool:
        """True if it says which request: a URL, path, query, regex or body criterion."""
        return bool(
            self.url_contains
            or self.path_contains
            or self.query_contains
            or self.url_regex
            or self.body_contains
            or self.response_contains
        )

    @model_validator(mode="after")
    def _has_criteria(self) -> RequestPredicate:
        if self.method is None and not self.locates:
            raise ValueError(
                "request predicate needs a method or a URL, path, query, regex or body criterion"
            )
        return self


class PageTextPredicate(_Pred):
    """Visible page text contains a phrase."""

    kind: Literal["page_text"] = "page_text"
    contains: PageText
    scope: Scope = Scope.FINAL


class UrlPredicate(_Pred):
    """The page URL matched."""

    kind: Literal["url"] = "url"
    contains: Substring | None = Field(
        default=None, description="Substring of the scheme, host and path (not the query)"
    )
    query_contains: Substring | None = Field(
        default=None, description="Substring of the decoded query string"
    )
    regex: Pattern | None = Field(default=None, description="Matched against the full URL")
    scope: Scope = Scope.FINAL

    @model_validator(mode="after")
    def _has_criteria(self) -> UrlPredicate:
        if self.contains is None and self.query_contains is None and self.regex is None:
            raise ValueError("url predicate needs 'contains', 'query_contains' or 'regex'")
        return self


class ActionPredicate(_Pred):
    """The agent performed a browser action."""

    kind: Literal["action"] = "action"
    name: NonBlank | None = Field(default=None, description="click, fill, goto, press, select, ...")
    target_contains: Substring | None = Field(
        default=None, description="Substring of the selector or target"
    )
    value_contains: NonBlank | None = None

    @model_validator(mode="after")
    def _has_criteria(self) -> ActionPredicate:
        if self.name is None and self.target_contains is None and self.value_contains is None:
            raise ValueError("action predicate needs a name, target_contains or value_contains")
        return self


class ValuePredicate(_Pred):
    """A value observed in evidence satisfies a comparison (e.g. price <= 30)."""

    kind: Literal["value"] = "value"
    field: NonBlank = Field(description="Human name of the value, e.g. 'price'")
    json_path: NonBlank | None = Field(
        default=None,
        description="Dotted path into a JSON request/response body, e.g. items.0.price",
    )
    in_request: RequestPredicate | None = Field(
        default=None, description="Which request carries the value; defaults to any mutation"
    )
    page_regex: Pattern | None = Field(
        default=None, description="Regex with one group capturing the value from page text"
    )
    op: Literal["==", "!=", "<", "<=", ">", ">=", "contains"] = "=="
    value: str | float

    @field_validator("value")
    @classmethod
    def _meaningful_value(cls, value: str | float, info: ValidationInfo) -> str | float:
        if not isinstance(value, str):
            return value
        return _substring(value) if info.data.get("op") == "contains" else _non_blank(value)

    @model_validator(mode="after")
    def _checkable(self) -> ValuePredicate:
        if self.json_path is None and self.page_regex is None:
            raise ValueError("value predicate needs 'json_path' or 'page_regex'")
        return self


class ScreenshotPredicate(_Pred):
    """A yes/no question answered by a vision model on the nearest screenshot. Last resort."""

    kind: Literal["screenshot"] = "screenshot"
    question: NonBlank
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


def vacuity_reason(
    predicate: RequestPredicate
    | PageTextPredicate
    | UrlPredicate
    | ActionPredicate
    | ValuePredicate
    | ScreenshotPredicate,
) -> str | None:
    """Why `predicate` is too weak to prove an agent's claim, or None if it is specific enough.

    A claim is untrusted, so its evidence must single something out: a request needs a method
    and a URL, path, query, regex or body criterion; an action needs a target or value; page
    text needs two words or a number ("1042", "$24.00"), because a single word such as
    "confirm" or "order" also appears on buttons and headings of pages that prove nothing.
    """
    match predicate:
        case RequestPredicate():
            if predicate.method is None or not predicate.locates:
                return (
                    "a request predicate needs a method and a URL, path, query, regex or body "
                    "criterion to prove a claim"
                )
        case ActionPredicate():
            if predicate.target_contains is None and predicate.value_contains is None:
                return (
                    "an action predicate needs target_contains or value_contains to prove a claim"
                )
        case PageTextPredicate():
            if len(_WORD.findall(predicate.contains)) < 2 and not _DIGIT.search(predicate.contains):
                return (
                    f"page text {predicate.contains!r} is a single word; it needs two words or a "
                    "number to prove a claim"
                )
        case _:
            pass
    return None
