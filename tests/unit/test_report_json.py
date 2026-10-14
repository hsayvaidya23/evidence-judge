from __future__ import annotations

import json
from pathlib import Path

from evidence_judge.core.verdict import SCHEMA_ID, Dimension, RunVerdict
from evidence_judge.report.json_report import (
    export_schema,
    schema_to_json,
    verdict_schema,
    verdict_to_json,
    write_json,
)
from tests.unit.test_report_fixtures import make_verdict

REPO_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "run-verdict.schema.json"


def test_round_trip(tmp_path: Path) -> None:
    verdict = make_verdict()
    path = write_json(verdict, tmp_path / "nested" / "verdict.json")
    restored = RunVerdict.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert restored == verdict


def test_schema_id_first_and_stable_dimension_order() -> None:
    text = verdict_to_json(make_verdict())
    data = json.loads(text)
    assert next(iter(data)) == "schema_id"
    assert data["schema_id"] == SCHEMA_ID
    order = [d.value for d in Dimension]
    keys = list(data["dimensions"])
    assert keys == sorted(keys, key=order.index)
    assert text.endswith("\n")
    assert text == verdict_to_json(make_verdict())


def test_schema_describes_run_verdict() -> None:
    schema = verdict_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["title"] == "RunVerdict"
    assert {"outcome", "score", "claims", "schema_id"} <= set(schema["properties"])


def test_export_schema_writes_file(tmp_path: Path) -> None:
    out = export_schema(tmp_path / "s.json")
    assert json.loads(out.read_text(encoding="utf-8")) == verdict_schema()


def test_committed_schema_is_up_to_date() -> None:
    assert REPO_SCHEMA.read_text(encoding="utf-8") == schema_to_json(), (
        "schemas/run-verdict.schema.json is stale; run `evidence-judge schema --out schemas/run-verdict.schema.json`"
    )
