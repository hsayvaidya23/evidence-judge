"""Spec validation problems reported with their place in the YAML."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_HINTS = {
    ("rerun.command", "list_type"): 'command must be a list of arguments, e.g. ["pytest", "-q"]',
    ("rerun", "extra_forbidden"): "rerun belongs in a session spec (session judge --rerun, "
    "session rerun), not a browser task spec",
}


class SpecProblem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    location: str = Field(description="Dotted path in the spec, e.g. expect.0.all.0.method")
    line: int | None = Field(default=None, description="1-based line of the location in the YAML")
    column: int | None = Field(default=None, description="1-based column of the location")
    message: str
    type: str = Field(description="yaml_syntax, yaml_alias, size, empty, or a pydantic error type")


class SpecError(ValueError):
    def __init__(self, problems: list[SpecProblem]) -> None:
        super().__init__(describe(problems))
        self.problems = problems


def compose(text: str) -> tuple[yaml.Node | None, object]:
    """The YAML node tree and the loaded data; a syntax error is a SpecError."""
    try:
        node = cast(yaml.Node | None, yaml.compose(text, Loader=yaml.SafeLoader))  # pyright: ignore[reportUnknownMemberType]
        return node, yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError([_yaml_problem(e)]) from e


def load_yaml[M: BaseModel](text: str, model: type[M]) -> M:
    """Parse and validate spec YAML; a SpecError lists each problem with its line and column."""
    node, data = compose(text)
    if data is None:
        raise SpecError(
            [SpecProblem(location="<root>", message="the spec file is empty", type="empty")]
        )
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise SpecError(validation_problems(e, node)) from e


def json_syntax_problem(data: bytes) -> str | None:
    """Why ``data`` is not JSON, as ``line L, column C: message``, or None when it parses. The
    message never quotes the data, which is not redacted yet."""
    try:
        json.loads(data)
    except json.JSONDecodeError as e:
        return f"line {e.lineno}, column {e.colno}: {e.msg.removesuffix(' at')}"
    except UnicodeDecodeError:
        return "not UTF-8 text"
    except RecursionError:
        return "nested too deeply"
    return None


def validation_problems(e: ValidationError, root: yaml.Node | None) -> list[SpecProblem]:
    return [
        _problem(err["loc"], err["msg"], err["type"], root)
        for err in e.errors(include_url=False, include_input=False, include_context=False)
    ]


def describe(problems: Sequence[SpecProblem]) -> str:
    """One problem per line, ``location (line N, column M): message``."""
    lines: list[str] = []
    for p in problems:
        where = ""
        if p.line is not None:
            where = f" (line {p.line}" + (f", column {p.column})" if p.column else ")")
        lines.append(f"{p.location}{where}: {p.message}")
    return "\n".join(lines)


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
    message = _HINTS.get((location, kind), message)
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
