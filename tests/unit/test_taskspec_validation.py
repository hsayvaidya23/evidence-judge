"""Spec errors are found when the spec loads, not when a run is judged."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_judge.core.predicates import RequestPredicate, UrlPredicate, ValuePredicate
from evidence_judge.core.taskspec import TaskSpec

HEADER = "id: x\ntask: t\n"


def _load(tmp_path: Path, body: str) -> TaskSpec:
    path = tmp_path / "spec.yaml"
    path.write_text(HEADER + body, encoding="utf-8")
    return TaskSpec.load(path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "expect:\n  - id: e\n    all:\n"
            "      - {kind: request, method: POST, url_contains: [/a], status: success}\n",
            "status",
        ),
        (
            "expect:\n  - id: e\n    all:\n"
            "      - {kind: request, method: POST, url_contains: [/a], status: 600}\n",
            "status",
        ),
        (
            "expect:\n  - id: e\n    all:\n      - {kind: request, method: POST, url_regex: '(oops'}\n",
            "invalid regex",
        ),
        ("expect:\n  - id: e\n    all:\n      - {kind: url, regex: '[a-'}\n", "invalid regex"),
        (
            "constraints:\n  - {kind: value, field: p, page_regex: '(a)(b)', value: 1}\n",
            "exactly one capture group",
        ),
        (
            "constraints:\n  - {kind: value, field: p, page_regex: 'Total \\d+', value: 1}\n",
            "exactly one capture group",
        ),
        (
            "constraints:\n  - {kind: value, field: p, page_regex: '(unclosed', value: 1}\n",
            "invalid regex",
        ),
        (
            "constraints:\n  - {kind: value, field: p, json_path: a, op: '<', value: .nan}\n",
            "finite",
        ),
        (
            "constraints:\n  - {kind: value, field: p, json_path: a, op: '<', value: .inf}\n",
            "finite",
        ),
        (
            "constraints:\n  - {kind: value, field: p, json_path: 'items[0', value: 1}\n",
            "json_path",
        ),
        (
            "constraints:\n  - {kind: value, field: p, json_path: 'items..price', value: 1}\n",
            "json_path",
        ),
        (
            "constraints:\n  - kind: value\n    field: p\n    json_path: a\n    value: 1\n"
            "    in_request: {kind: request, method: POST, url_regex: '(x'}\n",
            "invalid regex",
        ),
    ],
)
def test_malformed_spec_is_refused_at_load(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _load(tmp_path, body)


def test_long_regex_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="longer than"):
        UrlPredicate(regex="a" * 301)


@pytest.mark.parametrize("path", ["items.0.price", "items[0].price", "$.total", "a.b-c", "$"])
def test_valid_json_paths_load(path: str) -> None:
    assert ValuePredicate(field="p", json_path=path, value=1).json_path == path


def test_valid_regexes_load() -> None:
    assert RequestPredicate(method="GET", url_regex=r"/orders/\d+$").url_regex
    assert ValuePredicate(field="t", page_regex=r"Total:\s*\$([\d.,]+)", value=1).page_regex
    assert ValuePredicate(field="t", page_regex=r"(?:Total|Sum):\s*(\d+)", value=1).page_regex
