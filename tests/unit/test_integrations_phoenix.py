from __future__ import annotations

import builtins
import json
from typing import Any

import httpx
import pytest
from phoenix.client import Client

from evidence_judge.core.verdict import Usage
from evidence_judge.integrations import MissingExtraError
from evidence_judge.integrations import phoenix as px
from tests.unit.test_report_fixtures import make_verdict

SPAN = "00f067aa0ba902b7"


def _real_client_satisfies_the_protocol(client: Client) -> px.PhoenixClient:
    return client


def offline_verdict():
    return make_verdict().model_copy(update={"deterministic_share": 1.0, "usage": Usage()})


def test_annotations_cover_outcome_claims_and_dimensions() -> None:
    verdict = offline_verdict()
    annotations = px.span_annotations(verdict, span_id=SPAN)

    assert [(a["name"], a["identifier"]) for a in annotations] == [
        ("evidence_judge.outcome", "outcome"),
        ("evidence_judge.claim", "claim:c1"),
        ("evidence_judge.claim", "claim:c2"),
        ("evidence_judge.claim", "claim:c3"),
        ("evidence_judge.dimension.state", "dimension:state"),
        ("evidence_judge.dimension.success", "dimension:success"),
        ("evidence_judge.dimension.side_effects", "dimension:side_effects"),
    ]
    assert annotations[0] == {
        "name": "evidence_judge.outcome",
        "span_id": SPAN,
        "annotator_kind": "CODE",
        "result": {"label": "fail", "score": 0.42, "explanation": verdict.summary},
        "metadata": {
            "run_id": "run-42",
            "schema_id": "evidence-judge/run-verdict@1",
            "judge_version": "0.1.0",
        },
        "identifier": "outcome",
    }
    assert annotations[1]["result"] == {
        "label": "contradicted",
        "explanation": "POST /api/orders returned 500",
    }
    assert annotations[1]["metadata"]["evidence_ids"] == ["net#2"]
    assert annotations[4]["result"] == {
        "label": "not_assessable",
        "explanation": "No snapshots captured",
    }
    assert annotations[6]["result"] == {"score": 1.0}
    assert all(a["result"] for a in annotations)


def test_claims_can_be_left_out() -> None:
    names = [
        a["name"]
        for a in px.span_annotations(offline_verdict(), span_id=SPAN, include_claims=False)
    ]
    assert "evidence_judge.claim" not in names
    assert names[0] == "evidence_judge.outcome"


def test_annotator_kind_reflects_model_use() -> None:
    assert px.annotator_kind(offline_verdict()) == "CODE"
    assert px.annotator_kind(make_verdict()) == "LLM"


def test_rows_are_flat_for_a_dataframe() -> None:
    rows = px.span_annotation_rows(offline_verdict(), span_id=SPAN)
    assert rows[0] == {
        "span_id": SPAN,
        "name": "evidence_judge.outcome",
        "annotator_kind": "CODE",
        "label": "fail",
        "score": 0.42,
        "explanation": offline_verdict().summary,
        "metadata": {
            "run_id": "run-42",
            "schema_id": "evidence-judge/run-verdict@1",
            "judge_version": "0.1.0",
        },
        "identifier": "outcome",
    }
    assert rows[1]["score"] is None
    assert {tuple(sorted(r)) for r in rows} == {tuple(sorted(rows[0]))}


def test_push_posts_annotations_through_the_real_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    http = httpx.Client(base_url="http://phoenix.test", transport=httpx.MockTransport(handler))
    client = Client(base_url="http://phoenix.test", http_client=http)

    sent = px.push_to_phoenix(offline_verdict(), span_id=SPAN, client=client)

    [request] = requests
    assert request.method == "POST"
    assert request.url.path.endswith("/v1/span_annotations")
    assert json.loads(request.content) == {"data": sent}


def test_push_builds_a_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSpans:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def log_span_annotations(self, *, span_annotations: Any, sync: bool) -> None:
            self.calls.append({"span_annotations": list(span_annotations), "sync": sync})

    class FakeClient:
        def __init__(self) -> None:
            self.spans = FakeSpans()

    fake = FakeClient()
    monkeypatch.setattr(px, "_default_client", lambda: fake)
    sent = px.push_to_phoenix(offline_verdict(), span_id=SPAN, sync=True)
    assert fake.spans.calls == [{"span_annotations": sent, "sync": True}]


def test_missing_client_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_phoenix(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("phoenix"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_phoenix)
    with pytest.raises(MissingExtraError, match=r"evidence-judge\[phoenix\]"):
        px.push_to_phoenix(offline_verdict(), span_id=SPAN)
