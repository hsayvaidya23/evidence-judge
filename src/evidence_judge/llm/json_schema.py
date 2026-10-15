"""JSON schemas for structured-output engines other than the Anthropic API.

Pydantic schemas use keywords that strict structured-output engines reject or ignore
(`discriminator`, `oneOf`, `const`, `default`, string lengths). `portable_json_schema` rewrites
them into the subset those engines share; pydantic validation of the reply enforces the rest.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

_DROPPED = frozenset({"discriminator", "default", "title"})
_TO_DESCRIPTION = ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum")


def portable_json_schema(schema: type[BaseModel], *, all_required: bool) -> dict[str, Any]:
    """`schema`'s JSON schema with every object closed (`additionalProperties: false`),
    `const` pinned as a single-value `enum`, `oneOf` as `anyOf`, and bounds moved into the
    description.

    `all_required` lists every property as required, as OpenAI strict mode demands. Optional
    pydantic fields are nullable or have defaults, so a reply that fills them still validates.
    """
    return cast("dict[str, Any]", _rewrite(schema.model_json_schema(), all_required=all_required))


def _rewrite(node: Any, *, all_required: bool) -> Any:
    if isinstance(node, list):
        return [_rewrite(item, all_required=all_required) for item in cast("list[Any]", node)]
    if not isinstance(node, dict):
        return node
    source = cast("dict[str, Any]", node)
    out: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in source.items():
        if key in _DROPPED:
            continue
        if key in _TO_DESCRIPTION:
            notes.append(f"{key}: {value}")
            continue
        if key in ("properties", "$defs"):
            props = cast("dict[str, Any]", value)
            out[key] = {
                name: _rewrite(prop, all_required=all_required) for name, prop in props.items()
            }
            continue
        out["anyOf" if key == "oneOf" else key] = _rewrite(value, all_required=all_required)
    if "const" in out:
        out["enum"] = [out.pop("const")]
    if notes:
        description = out.get("description")
        joined = "; ".join(notes)
        out["description"] = f"{description} ({joined})" if description else joined
    if out.get("type") == "object" or "properties" in out:
        props = cast("dict[str, Any]", out.get("properties", {}))
        out.setdefault("properties", props)
        out["additionalProperties"] = False
        required = list(props) if all_required else cast("list[str]", out.get("required", []))
        constant = [
            name for name, prop in props.items() if "enum" in prop and len(prop["enum"]) == 1
        ]
        merged = list(dict.fromkeys([*required, *constant]))
        if merged:
            out["required"] = merged
    return out
