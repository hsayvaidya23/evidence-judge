from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.checks.screenshot import ScreenshotAnswer
from evidence_judge.extract.schema import ClaimExtraction
from evidence_judge.llm.json_schema import portable_json_schema


class Inner(BaseModel):
    kind: Literal["inner"] = "inner"
    note: str | None = None


class Outer(BaseModel):
    model_config = ConfigDict(title="Custom")

    name: str = Field(min_length=1, max_length=5, description="Short name")
    count: int = Field(default=0, ge=0)
    inner: Inner
    items: list[Inner] = Field(default_factory=list[Inner])


def _walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk(value))
    return found


def test_objects_are_closed_and_optional_fields_stay_optional():
    schema = portable_json_schema(Outer, all_required=False)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name", "inner"]
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["kind"]
    assert inner["properties"]["kind"] == {"type": "string", "enum": ["inner"]}


def test_all_required_lists_every_property():
    schema = portable_json_schema(Outer, all_required=True)
    assert schema["required"] == ["name", "count", "inner", "items"]
    assert schema["$defs"]["Inner"]["required"] == ["kind", "note"]


def test_unsupported_keywords_are_dropped_or_described():
    schema = portable_json_schema(Outer, all_required=True)
    nodes = _walk(schema)
    for keyword in ("default", "title", "const", "discriminator", "oneOf", "minLength"):
        assert not any(keyword in node for node in nodes), keyword
    name = schema["properties"]["name"]
    assert name["description"].startswith("Short name (")
    assert "minLength: 1" in name["description"]
    assert "maxLength: 5" in name["description"]
    assert schema["properties"]["count"]["description"] == "minimum: 0"


def test_a_property_called_title_or_default_is_kept():
    class Named(BaseModel):
        title: str
        default: int

    schema = portable_json_schema(Named, all_required=True)
    assert set(schema["properties"]) == {"title", "default"}
    assert schema["required"] == ["title", "default"]


def test_discriminated_unions_become_any_of():
    schema = portable_json_schema(ClaimExtraction, all_required=True)
    text = json.dumps(schema)
    assert "oneOf" not in text
    assert "discriminator" not in text
    assert '"anyOf"' in text
    for node in _walk(schema):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])


def test_screenshot_answer_schema():
    assert portable_json_schema(ScreenshotAnswer, all_required=True) == {
        "additionalProperties": False,
        "properties": {
            "answer": {"enum": ["yes", "no", "unclear"], "type": "string"},
            "reason": {"description": "One sentence naming what is visible", "type": "string"},
        },
        "required": ["answer", "reason"],
        "type": "object",
    }


def test_replies_that_fill_optional_fields_still_validate():
    reply = {"name": "ab", "count": 1, "inner": {"kind": "inner", "note": None}, "items": []}
    assert Outer.model_validate(reply).inner.note is None
