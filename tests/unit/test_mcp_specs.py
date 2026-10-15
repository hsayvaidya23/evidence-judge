from __future__ import annotations

from pathlib import Path

from evidence_judge.mcp.specs import MAX_SPEC_BYTES, describe, parse_spec

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests" / "fixtures" / "runs" / "honest_success" / "spec.yaml"


def test_a_valid_spec_is_summarised() -> None:
    spec, check = parse_spec(SPEC.read_text(encoding="utf-8"))
    assert spec is not None
    assert check.valid
    assert check.spec_id == spec.id
    assert (check.expectations, check.forbids, check.constraints, check.allowed) == (2, 1, 2, 1)
    assert check.errors == []
    assert describe(check) == ""


def test_errors_point_at_their_yaml_line() -> None:
    text = (
        "id: x\n"
        "task: t\n"
        "expect:\n"
        "  - id: a\n"
        "    all:\n"
        "      - kind: request\n"
        "        method: POST\n"
        "        status: nope\n"
        "        url_contains: [/x]\n"
    )
    spec, check = parse_spec(text)
    assert spec is None
    assert not check.valid
    [problem] = check.errors
    assert problem.location.startswith("expect.0.all.0")
    assert problem.location.endswith("status")
    assert (problem.line, problem.column) == (8, 17)
    assert "status" in describe(check)
    assert "(line 8, column 17)" in describe(check)


def test_a_missing_field_points_at_its_mapping() -> None:
    _, check = parse_spec("id: x\n")
    [problem] = check.errors
    assert (problem.location, problem.type, problem.line) == ("task", "missing", 1)


def test_a_union_tag_in_the_location_is_skipped() -> None:
    text = "id: x\ntask: t\nexpect:\n  - id: a\n    all:\n      - {kind: request}\n"
    _, check = parse_spec(text)
    [problem] = check.errors
    assert problem.location == "expect.0.all.0.request"
    assert (problem.line, problem.column) == (6, 9)


def test_spec_level_errors_are_at_the_root() -> None:
    text = (
        "id: x\ntask: t\nexpect:\n  - id: a\n    all:\n"
        "      - {kind: request, url_contains: [/x]}\n"
    )
    _, check = parse_spec(text)
    [problem] = check.errors
    assert problem.location == "<root>"
    assert "request predicate needs a method" in problem.message


def test_yaml_syntax_errors_have_a_position() -> None:
    _, check = parse_spec("id: [\n")
    [problem] = check.errors
    assert problem.type == "yaml_syntax"
    assert problem.line == 2


def test_a_non_mapping_spec_is_invalid() -> None:
    _, check = parse_spec("- just\n- a list\n")
    assert not check.valid
    assert check.errors[0].location == "<root>"


def test_aliases_are_refused() -> None:
    _, check = parse_spec("id: &a x\ntask: *a\n")
    [problem] = check.errors
    assert problem.type == "yaml_alias"
    assert problem.line == 1


def test_oversized_specs_are_not_parsed() -> None:
    _, check = parse_spec("#" * (MAX_SPEC_BYTES + 1))
    assert [p.type for p in check.errors] == ["size"]
