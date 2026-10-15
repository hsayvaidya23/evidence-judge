"""Task spec validation that reports each problem with its place in the YAML."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidence_judge.core.taskspec import TaskSpec

MAX_SPEC_BYTES = 1024 * 1024
"""Specs are short documents; a larger one is refused before it is parsed."""


class SpecProblem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    location: str = Field(description="Dotted path in the spec, e.g. expect.0.all.0.method")
    line: int | None = Field(default=None, description="1-based line of the location in the YAML")
    column: int | None = Field(default=None, description="1-based column of the location")
    message: str
    type: str = Field(description="yaml_syntax, yaml_alias, size, or a pydantic error type")


class SpecCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    spec_id: str | None = None
    task: str | None = None
    expectations: int = Field(default=0, description="Number of expect entries")
    forbids: int = 0
    constraints: int = 0
    allowed: int = Field(default=0, description="Number of allow entries")
    site: str | None = None
    errors: list[SpecProblem] = Field(default_factory=list[SpecProblem])


def parse_spec(text: str) -> tuple[TaskSpec | None, SpecCheck]:
    """Parse and validate spec YAML; the spec is None when the check lists errors."""
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        return None, _invalid(
            SpecProblem(
                location="<root>",
                message=f"spec exceeds {MAX_SPEC_BYTES} bytes",
                type="size",
            )
        )
    try:
        node = cast(yaml.Node | None, yaml.compose(text, Loader=yaml.SafeLoader))  # pyright: ignore[reportUnknownMemberType]
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, _invalid(_yaml_problem(e))
    alias = _first_alias(node)
    if alias is not None:
        return None, _invalid(
            SpecProblem(
                location="<root>",
                line=alias.start_mark.line + 1,
                column=alias.start_mark.column + 1,
                message="YAML anchors and aliases are not supported in task specs",
                type="yaml_alias",
            )
        )
    try:
        spec = TaskSpec.model_validate(data)
    except ValidationError as e:
        problems = [
            _problem(err["loc"], err["msg"], err["type"], node)
            for err in e.errors(include_url=False, include_input=False, include_context=False)
        ]
        return None, SpecCheck(valid=False, errors=problems)
    return spec, SpecCheck(
        valid=True,
        spec_id=spec.id,
        task=spec.task,
        expectations=len(spec.expect),
        forbids=len(spec.forbid),
        constraints=len(spec.constraints),
        allowed=len(spec.allow),
        site=spec.site,
    )


def describe(check: SpecCheck) -> str:
    """One problem per line, ``location (line N): message``."""
    lines: list[str] = []
    for p in check.errors:
        where = f" (line {p.line})" if p.line is not None else ""
        lines.append(f"{p.location}{where}: {p.message}")
    return "\n".join(lines)


def _invalid(problem: SpecProblem) -> SpecCheck:
    return SpecCheck(valid=False, errors=[problem])


def _first_alias(root: yaml.Node | None) -> yaml.Node | None:
    """A node reached twice: an alias. Refusing them keeps alias bombs out of validation."""
    seen: set[int] = set()
    stack: list[yaml.Node] = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if id(node) in seen:
            return node
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return None


def _yaml_problem(e: yaml.YAMLError) -> SpecProblem:
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None)
    message = str(problem) if problem else type(e).__name__
    line = column = None
    if isinstance(mark, yaml.Mark):
        line, column = mark.line + 1, mark.column + 1
    return SpecProblem(
        location="<root>", line=line, column=column, message=message, type="yaml_syntax"
    )


def _problem(
    loc: Sequence[int | str], message: str, kind: str, root: yaml.Node | None
) -> SpecProblem:
    location = ".".join(str(p) for p in loc) or "<root>"
    node = _locate(root, loc)
    if node is None:
        return SpecProblem(location=location, message=message, type=kind)
    mark = node.start_mark
    return SpecProblem(
        location=location,
        line=mark.line + 1,
        column=mark.column + 1,
        message=message,
        type=kind,
    )


def _locate(node: yaml.Node | None, loc: Sequence[int | str]) -> yaml.Node | None:
    """The deepest YAML node on ``loc``. Path parts with no node (a union's tag, a missing key)
    are skipped, so a missing field points at the mapping that lacks it."""
    for part in loc:
        child = _child(node, part)
        if child is not None:
            node = child
    return node


def _child(node: yaml.Node | None, part: int | str) -> yaml.Node | None:
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == str(part):
                return value
    if (
        isinstance(node, yaml.SequenceNode)
        and isinstance(part, int)
        and 0 <= part < len(node.value)
    ):
        return node.value[part]
    return None
