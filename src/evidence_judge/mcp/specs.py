"""Task spec validation that reports each problem with its place in the YAML."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidence_judge.core import spec_errors
from evidence_judge.core.spec_errors import SpecError, SpecProblem
from evidence_judge.core.taskspec import TaskSpec

MAX_SPEC_BYTES = 1024 * 1024
"""Specs are short documents; a larger one is refused before it is parsed."""


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
        node, data = spec_errors.compose(text)
    except SpecError as e:
        return None, SpecCheck(valid=False, errors=e.problems)
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
        return None, SpecCheck(valid=False, errors=spec_errors.validation_problems(e, node))
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
    """One problem per line, ``location (line N, column M): message``."""
    return spec_errors.describe(check.errors)


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
