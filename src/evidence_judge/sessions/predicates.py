"""Typed predicates over a coding-agent session, the claims they prove, and task specs.

A claim compiles to predicates that must all hold. An LLM may propose them; only the checkers
in `sessions.checks` evaluate them. Strings are validated when a predicate is built, so a
predicate every session satisfies (an empty substring, a `**` glob) cannot prove anything.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from evidence_judge.core.claims import ClaimKind
from evidence_judge.core.predicates import compile_pattern
from evidence_judge.ingest._json import as_object
from evidence_judge.sessions.models import Category
from evidence_judge.sessions.rerun import RerunSpec
from evidence_judge.sessions.shell import analyse

MIN_TEXT = 3
_GLOB_LITERAL = re.compile(r"[^*?/.]")
_GLOB_CLASS = re.compile(r"\[!?\]?[^\]]*\]")
_GLOB_UNSUPPORTED = re.compile(r"[{}\\\[]")


def _text(value: str) -> str:
    stripped = value.strip()
    if len(stripped) < MIN_TEXT:
        raise ValueError(f"{stripped!r} is shorter than {MIN_TEXT} characters and matches too much")
    return stripped


def _regex(value: str) -> str:
    if not value.strip():
        raise ValueError("regex must not be empty")
    compile_pattern(value)
    return value


def _glob(value: str) -> str:
    stripped = value.strip().removeprefix("./")
    if not stripped:
        raise ValueError("glob must not be empty")
    if found := _GLOB_UNSUPPORTED.search(_GLOB_CLASS.sub("", stripped)):
        what = "an unclosed '['" if found[0] == "[" else repr(found[0])
        raise ValueError(
            f"glob {stripped!r} has {what}; globs support *, **, ? and [...] / [!...] classes, "
            "not {a,b} alternatives or backslash escapes (list each path as its own glob)"
        )
    return stripped


def broad(glob: str) -> bool:
    """True for a glob that names no file in particular (`*`, `**`, `**/*.*`, `[!_]*`)."""
    return len(_GLOB_LITERAL.findall(_GLOB_CLASS.sub("", glob))) < 2


Text = Annotated[str, AfterValidator(_text)]
Regex = Annotated[str, AfterValidator(_regex)]
Glob = Annotated[str, AfterValidator(_glob)]


class TestResultPredicate(BaseModel):
    """The test runs after the last workspace change show `expect`."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    __test__ = False

    kind: Literal["test_result"] = "test_result"
    expect: Literal["pass", "fail"] = "pass"
    whole_suite: bool = Field(
        default=True, description="Only a run without filters can prove the whole suite passes"
    )
    runner: str | None = Field(default=None, description="e.g. pytest, jest, go test")
    mentions: Text | None = Field(
        default=None, description="A test the claim names; a run must mention it"
    )
    min_tests: int | None = Field(
        default=None,
        ge=1,
        description="A run proves the whole suite only if it ran at least this many tests",
    )
    count: int | None = Field(
        default=None,
        ge=1,
        description="For expect fail: how many tests the claim says fail; the run must show "
        "exactly that many",
    )

    @model_validator(mode="after")
    def _count_fails(self) -> Self:
        if self.count is not None and self.expect != "fail":
            raise ValueError("count is the number of failing tests; it needs expect: fail")
        return self


class CommandPredicate(BaseModel):
    """A command matching `regex`, `contains` or `category` ran (with that status, in that
    scope)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command"] = "command"
    regex: Regex | None = None
    contains: Text | None = None
    category: Literal["test", "build", "lint", "typecheck", "format"] | None = Field(
        default=None, description="Any command the judge recognises as this kind of step"
    )
    ok: bool | None = Field(default=None, description="True: exit 0; False: non-zero")
    exit_code: int | None = None
    scope: Literal["any", "last", "after_last_edit"] = "any"

    @model_validator(mode="after")
    def _one_matcher(self) -> Self:
        matchers = [m for m in (self.regex, self.contains, self.category) if m is not None]
        if len(matchers) != 1:
            raise ValueError("a command predicate needs exactly one of regex, contains or category")
        if self.ok is not None and self.exit_code is not None:
            raise ValueError("use ok or exit_code, not both")
        return self


class EditPredicate(BaseModel):
    """Files matching `path` (or any test file) were, or were not, changed; or, with `only`, no
    file outside those globs was."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["edit"] = "edit"
    path: Glob | None = Field(
        default=None,
        description="Glob relative to the session directory; without '/' it matches the file "
        "name at any depth",
    )
    test_files: bool = Field(default=False, description="Match every test file instead of a glob")
    only: tuple[Glob, ...] | None = Field(
        default=None,
        min_length=1,
        description="Globs every file the session changed must match ('I only changed X')",
    )
    touched: bool = True
    change: Literal["add", "modify", "delete", "any"] = "any"

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        targets = (self.path is not None, self.test_files, self.only is not None)
        if sum(targets) != 1:
            raise ValueError("an edit predicate needs exactly one of path, test_files or only")
        if self.only is not None:
            if not self.touched or self.change != "any":
                raise ValueError("only takes neither touched nor change")
            if wide := next((g for g in self.only if broad(g)), None):
                raise ValueError(f"glob {wide!r} names nothing specific; every edit would match")
        if self.touched and self.path is not None and broad(self.path):
            raise ValueError(
                f"glob {self.path!r} names nothing specific; any edit at all would prove it"
            )
        return self


class BuildPredicate(BaseModel):
    """The last build, lint, type check or format check after the last change exited as
    expected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["build"] = "build"
    category: Literal["build", "lint", "typecheck", "format"] = "build"
    ok: bool = True


SessionPredicate = Annotated[
    TestResultPredicate | CommandPredicate | EditPredicate | BuildPredicate,
    Field(discriminator="kind"),
]
PREDICATE_ADAPTER: TypeAdapter[SessionPredicate] = TypeAdapter(SessionPredicate)


_CHECKS: Final = frozenset({"test", "build", "lint", "typecheck", "format"})


def claimed(predicate: SessionPredicate) -> SessionPredicate:
    """`predicate` as a claim states it: a claim that the agent ran a command says the command
    succeeded, unless the command is a test, build, lint, type or format check, whose failure is
    a result the answer reports in its own words. Specs keep `ok` unset to mean "ran"."""
    if not isinstance(predicate, CommandPredicate) or predicate.ok is not None:
        return predicate
    if predicate.exit_code is not None or predicate.category is not None:
        return predicate
    if predicate.contains is not None and _CHECKS & {
        c.value for c in analyse(predicate.contains).categories
    }:
        return predicate
    return predicate.model_copy(update={"ok": True})


class SessionClaim(BaseModel):
    """One checkable statement from the agent's answer. Every predicate must hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    quote: str = Field(description="Verbatim span of the agent's answer")
    kind: ClaimKind
    key: bool = False
    predicates: list[SessionPredicate] = Field(min_length=1, max_length=4)

    @field_validator("predicates")
    @classmethod
    def _as_claimed(cls, predicates: list[SessionPredicate]) -> list[SessionPredicate]:
        return [claimed(p) for p in predicates]


class SessionClaimSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[SessionClaim] = Field(default_factory=list[SessionClaim])
    extractor: str = "unknown"
    note: str | None = None
    unchecked: list[str] = Field(
        default_factory=list[str],
        description="Claims the extractor proposed but could not keep whole: no valid predicate, "
        "a quote not in the answer, a predicate that did not validate, or over the claim cap",
    )


class SpecItem(BaseModel):
    """A spec expectation: an id, an optional description and one predicate, written flat in
    YAML (`{id: suite-green, kind: test_result, expect: pass}`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str | None = None
    predicate: SessionPredicate

    @model_validator(mode="before")
    @classmethod
    def _flat(cls, data: object) -> object:
        flat = dict(as_object(data))
        if flat and "predicate" not in flat:
            meta = {k: flat.pop(k) for k in ("id", "description") if k in flat}
            return {**meta, "predicate": flat}
        return data


class SessionSpec(BaseModel):
    """What the person who ran the agent expects, independent of what the agent said."""

    model_config = ConfigDict(extra="forbid")

    task: str | None = None
    cwd: str | None = Field(
        default=None,
        description="The session directory (an absolute path), for a record that has none (a "
        "Codex `exec --json` stream): paths are then placed in the project exactly",
    )
    expect: list[SpecItem] = Field(default_factory=list[SpecItem])
    forbid: list[SpecItem] = Field(default_factory=list[SpecItem])
    ignore_edits: list[Glob] = Field(
        default_factory=list[str],
        description="Edits to these files (docs, changelogs) do not make earlier test runs stale",
    )
    require_tamper_evidence: bool = Field(
        default=False,
        description="Cap the run at needs review unless it comes from an evidence log",
    )
    trusted_evidence_log: bool = Field(
        default=False,
        description="The evidence log, its key and head anchor were out of the agent's reach "
        "(sandbox, container or another OS user); only then can a verified log be full tamper "
        "evidence",
    )
    rerun: RerunSpec | None = Field(
        default=None,
        description="Run the suite again under the judge's control (`session judge --rerun`) "
        "and decide test results from its report instead of the session record",
    )
    trusted_wrappers: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="What a project wrapper runs, as its owner declares it (`make test: pytest "
        "-q`): a run through it whose command the session does not show is judged as that "
        "command, until the session may have changed the wrapper's definition",
    )

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str | None) -> str | None:
        if value is not None and not posixpath.isabs(value):
            raise ValueError(f"cwd must be an absolute path, not {value!r}")
        return None if value is None else posixpath.normpath(value)

    @field_validator("trusted_wrappers")
    @classmethod
    def _wrappers(cls, value: dict[str, str]) -> dict[str, str]:
        for wrapper, command in value.items():
            if analyse(wrapper).wrapper is None:
                raise ValueError(f"{wrapper!r} is not a wrapper the judge recognises")
            declared = analyse(command)
            checks = {Category.LINT, Category.TYPECHECK} & set(declared.categories)
            if declared.parse_error or declared.wrapper or not (declared.runners or checks):
                raise ValueError(
                    f"{command!r} must be a test, lint or type-check command, not another wrapper"
                )
        return value

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [item.id for item in (*self.expect, *self.forbid)]
        if len(ids) != len(set(ids)):
            raise ValueError("expectation ids must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> SessionSpec:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("a session spec must be a YAML mapping")
        return cls.model_validate(data)


def describe(predicate: SessionPredicate) -> str:
    match predicate:
        case TestResultPredicate():
            if predicate.count is not None and not predicate.mentions:
                plural = predicate.count > 1
                return f"{predicate.count} test{'s' if plural else ''} fail{'' if plural else 's'}"
            if predicate.mentions:
                scope = f"test {predicate.mentions}"
            else:
                scope = "the whole suite" if predicate.whole_suite else "the tests that ran"
            singular = bool(predicate.mentions) or predicate.whole_suite
            passing = predicate.expect == "pass"
            verb = {True: "passes", False: "fails"} if singular else {True: "pass", False: "fail"}
            return f"{scope} {verb[passing]}"
        case CommandPredicate():
            target = predicate.contains or f"/{predicate.regex}/"
            return f"ran {target!r}" + (
                ""
                if predicate.ok is None
                else (" successfully" if predicate.ok else " and it failed")
            )
        case EditPredicate():
            if predicate.only is not None:
                return f"only {', '.join(predicate.only)} changed"
            target = predicate.path or "test files"
            return f"{target} {'changed' if predicate.touched else 'untouched'}"
        case BuildPredicate():
            return f"{predicate.category} {'succeeds' if predicate.ok else 'fails'}"
