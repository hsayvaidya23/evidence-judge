"""An Inspect AI scorer that grades each sample's recorded browser run with Evidence Judge.

Needs the ``inspect`` extra. Each sample's ``metadata`` names the recording::

    Sample(input="Buy a red kettle under $30", metadata={
        "trace": "runs/42/trace.zip",   # and/or "har", "browser_use"
        "spec": "runs/42/spec.yaml",    # optional
    })

Optional keys: ``answer`` / ``answer_file`` (default: the model's final completion),
``reasoning_file``, ``task`` (default: the spec's task, then the sample input) and ``run_id``.
Relative paths resolve against ``base_dir``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from evidence_judge.config import Settings, stable_redaction_key
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run
from evidence_judge.integrations._common import MissingExtraError
from evidence_judge.llm.factory import build_llm
from evidence_judge.redact import Redactor

try:
    from inspect_ai.scorer import (
        CORRECT,
        INCORRECT,
        NOANSWER,
        Score,
        Scorer,
        Target,
        accuracy,
        scorer,
        stderr,
    )
    from inspect_ai.solver import TaskState
except ImportError as e:
    raise MissingExtraError("Inspect AI", "inspect", e) from e

EVIDENCE_KEYS = ("trace", "har", "browser_use")
PATH_KEYS = (*EVIDENCE_KEYS, "spec", "answer_file", "reasoning_file")


def outcome_value(outcome: Outcome, needs_review: str = NOANSWER) -> str:
    """Inspect grade for an outcome: pass is C, fail is I, needs review is ``needs_review``."""
    if outcome is Outcome.PASS:
        return CORRECT
    if outcome is Outcome.FAIL:
        return INCORRECT
    return needs_review


def _metadata_str(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str | Path):
        raise ValueError(f"sample metadata {key!r} must be a string or path")
    return str(value)


def _resolve(base_dir: Path | None, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if base_dir is None or path.is_absolute() else base_dir / path


def _load(
    metadata: Mapping[str, Any],
    state_input: str,
    completion: str,
    base_dir: Path | None,
    settings: Settings,
) -> tuple[Run, TaskSpec | None]:
    paths = {key: _resolve(base_dir, _metadata_str(metadata, key)) for key in PATH_KEYS}
    if not any(paths[key] for key in EVIDENCE_KEYS):
        raise ValueError(
            "sample metadata names no evidence: set 'trace', 'har' and/or 'browser_use'"
        )
    spec = TaskSpec.load(paths["spec"]) if paths["spec"] else None
    answer = (
        None if paths["answer_file"] else (_metadata_str(metadata, "answer") or completion or None)
    )
    task = _metadata_str(metadata, "task") or (spec.task if spec else None) or state_input or None
    run = load_run(
        trace=paths["trace"],
        har=paths["har"],
        browser_use=paths["browser_use"],
        answer=answer,
        answer_file=paths["answer_file"],
        reasoning_file=paths["reasoning_file"],
        task=task,
        run_id=_metadata_str(metadata, "run_id"),
        max_body_chars=settings.max_body_chars,
    )
    return run, spec


def _redact(settings: Settings, run: Run, spec: TaskSpec | None) -> tuple[Run, TaskSpec | None]:
    key = stable_redaction_key(settings)
    redactor = Redactor(key) if key is not None else Redactor.from_settings(settings)
    run, _ = redactor.redact_run(run)
    if spec is not None:
        spec, _ = redactor.redact_model(spec)
    return run, spec


def to_score(
    verdict: RunVerdict,
    run: Run,
    *,
    value: Literal["outcome", "score"] = "outcome",
    needs_review: str = NOANSWER,
) -> Score:
    """An Inspect ``Score`` for a verdict; the full verdict JSON is the score's metadata."""
    return Score(
        value=outcome_value(verdict.outcome, needs_review) if value == "outcome" else verdict.score,
        answer=run.agent_answer,
        explanation=verdict.summary,
        metadata=verdict.model_dump(mode="json"),
    )


@scorer(metrics=[accuracy(), stderr()], name="evidence_judge")
def evidence_judge_scorer(
    *,
    value: Literal["outcome", "score"] = "outcome",
    needs_review: str = NOANSWER,
    offline: bool = False,
    redact: bool = True,
    base_dir: str | Path | None = None,
    settings: Settings | None = None,
    judge: Judge | None = None,
) -> Scorer:
    """Score each sample by judging the browser run its metadata points to.

    ``value="outcome"`` gives C (pass), I (fail) or ``needs_review`` (default N, which counts as
    0 in accuracy); ``value="score"`` gives the 0..1 verdict score. Without ``offline`` an LLM is
    used for claim extraction when ``ANTHROPIC_API_KEY`` is set. ``settings`` defaults to
    ``Settings()`` (``EVJ_*`` environment variables); ``judge`` overrides the judge entirely.
    """
    root = Path(base_dir) if base_dir is not None else None
    active_settings = settings
    active_judge = judge

    async def score(state: TaskState, target: Target) -> Score:
        nonlocal active_settings, active_judge
        if active_settings is None:
            active_settings = Settings()
        if active_judge is None:
            active_judge = Judge(active_settings, None if offline else build_llm(active_settings))
        run, spec = await asyncio.to_thread(
            _load, state.metadata, state.input_text, state.output.completion, root, active_settings
        )
        if redact and active_settings.redact:
            run, spec = _redact(active_settings, run, spec)
        verdict = await active_judge.judge(run, JudgeOptions(offline=offline, spec=spec))
        return to_score(verdict, run, value=value, needs_review=needs_review)

    return score
