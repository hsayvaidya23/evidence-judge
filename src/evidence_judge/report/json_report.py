from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evidence_judge.core.verdict import Dimension, RunVerdict

SCHEMA_URL = "https://raw.githubusercontent.com/hsayvaidya23/evidence-judge/main/schemas/run-verdict.schema.json"
_DIMENSION_ORDER = {d.value: i for i, d in enumerate(Dimension)}


def verdict_to_dict(verdict: RunVerdict) -> dict[str, Any]:
    data = verdict.model_dump(mode="json")
    dims: dict[str, Any] = data["dimensions"]
    data["dimensions"] = {k: dims[k] for k in sorted(dims, key=_DIMENSION_ORDER.__getitem__)}
    return data


def verdict_to_json(verdict: RunVerdict) -> str:
    return json.dumps(verdict_to_dict(verdict), indent=2, ensure_ascii=False) + "\n"


def write_json(verdict: RunVerdict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(verdict_to_json(verdict), encoding="utf-8")
    return path


def verdict_schema() -> dict[str, Any]:
    schema = RunVerdict.model_json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_URL,
        **schema,
    }


def schema_to_json() -> str:
    return json.dumps(verdict_schema(), indent=2, ensure_ascii=False) + "\n"


def export_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_to_json(), encoding="utf-8")
    return path
