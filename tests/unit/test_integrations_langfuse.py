from __future__ import annotations

import builtins
import uuid
from typing import Any

import pytest
from langfuse import Langfuse

from evidence_judge.integrations import MissingExtraError
from evidence_judge.integrations import langfuse as lf
from tests.unit.test_report_fixtures import make_verdict

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


class FakeLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.flushed = 0

    def create_score(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def flush(self) -> None:
        self.flushed += 1


def _real_client_satisfies_the_protocol(client: Langfuse) -> lf.ScoreClient:
    return client


def test_pushes_score_outcome_and_assessable_dimensions() -> None:
    verdict = make_verdict()
    client = FakeLangfuse()

    lf.push_to_langfuse(verdict, trace_id=TRACE, client=client)

    metadata = {
        "run_id": "run-42",
        "schema_id": "evidence-judge/run-verdict@1",
        "judge_version": "0.1.0",
    }
    by_name = {c["name"]: c for c in client.calls}
    assert list(by_name) == [
        "evidence_judge.score",
        "evidence_judge.outcome",
        "evidence_judge.dimension.success",
        "evidence_judge.dimension.side_effects",
    ]
    assert by_name["evidence_judge.score"] == {
        "name": "evidence_judge.score",
        "value": 0.42,
        "trace_id": TRACE,
        "observation_id": None,
        "score_id": lf.score_id(run_id="run-42", name="evidence_judge.score", trace_id=TRACE),
        "data_type": "NUMERIC",
        "comment": verdict.summary,
        "metadata": metadata,
    }
    assert by_name["evidence_judge.outcome"]["value"] == "fail"
    assert by_name["evidence_judge.outcome"]["data_type"] == "CATEGORICAL"
    assert by_name["evidence_judge.dimension.success"]["value"] == 0.0
    assert by_name["evidence_judge.dimension.success"]["comment"] == "Order failed"
    assert by_name["evidence_judge.dimension.side_effects"]["comment"] is None
    assert client.flushed == 0


def test_score_ids_are_deterministic_and_unique() -> None:
    first, second = FakeLangfuse(), FakeLangfuse()
    lf.push_to_langfuse(make_verdict(), trace_id=TRACE, observation_id="obs-1", client=first)
    lf.push_to_langfuse(make_verdict(), trace_id=TRACE, observation_id="obs-1", client=second)

    ids = [c["score_id"] for c in first.calls]
    assert ids == [c["score_id"] for c in second.calls]
    assert len(set(ids)) == len(ids)
    assert all(uuid.UUID(i).version == 5 for i in ids)
    assert all(c["observation_id"] == "obs-1" for c in first.calls)

    other_trace = FakeLangfuse()
    lf.push_to_langfuse(make_verdict(), trace_id="0" * 31 + "1", client=other_trace)
    assert not set(ids) & {c["score_id"] for c in other_trace.calls}


def test_builds_and_flushes_a_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeLangfuse] = []

    def factory() -> FakeLangfuse:
        created.append(FakeLangfuse())
        return created[-1]

    monkeypatch.setattr(lf, "_client_class", lambda: factory)
    scores = lf.push_to_langfuse(make_verdict(), trace_id=TRACE)

    [client] = created
    assert client.flushed == 1
    assert [s.id for s in scores] == [c["score_id"] for c in client.calls]


def test_ensure_configured_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGFUSE_SECRET_KEY"):
        lf.ensure_configured()
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    lf.ensure_configured()


def test_missing_sdk_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_langfuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "langfuse":
            raise ImportError("No module named 'langfuse'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_langfuse)
    with pytest.raises(MissingExtraError, match=r"evidence-judge\[langfuse\]"):
        lf.push_to_langfuse(make_verdict(), trace_id=TRACE)
