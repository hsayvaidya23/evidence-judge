"""Redact a session's free text (commands, outputs, the task and the answer) with the same
`Redactor` the browser judge uses, so secrets in `export TOKEN=...` lines, bearer headers or
test output never reach a model or a report. Analysis done at ingest is kept: it was computed
from the raw strings and is all enums, counts and flags."""

from __future__ import annotations

from collections import Counter

from evidence_judge.redact import RedactionReport, Redactor
from evidence_judge.sessions.models import CommandEvent, FileEditEvent, SessionEvent, SessionRun
from evidence_judge.sessions.predicates import SessionClaimSet, SessionSpec


class _Text:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self.counts: Counter[str] = Counter()

    def __call__(self, text: str) -> str:
        out, report = self._redactor.redact_text(text)
        self.counts.update(report.counts)
        return out

    def opt(self, text: str | None) -> str | None:
        return None if text is None else self(text)


def _event(event: SessionEvent, text: _Text) -> SessionEvent:
    if isinstance(event, CommandEvent):
        analysis = event.analysis.model_copy(
            update={
                "deleted_paths": [text(p) for p in event.analysis.deleted_paths],
                "touched_paths": [text(p) for p in event.analysis.touched_paths],
            }
        )
        return event.model_copy(
            update={
                "command": text(event.command),
                "output": text(event.output),
                "analysis": analysis,
            }
        )
    if isinstance(event, FileEditEvent):
        return event.model_copy(update={"test_findings": [text(f) for f in event.test_findings]})
    return event


def redact_session(redactor: Redactor, run: SessionRun) -> tuple[SessionRun, RedactionReport]:
    text = _Text(redactor)
    redacted = run.model_copy(
        update={
            "task": text.opt(run.task),
            "final_answer": text.opt(run.final_answer),
            "events": [_event(e, text) for e in run.events],
        }
    )
    return redacted, RedactionReport(counts=dict(text.counts))


def redact_claims(redactor: Redactor, claims: SessionClaimSet) -> SessionClaimSet:
    redacted, _ = redactor.redact_model(claims)
    return redacted


def redact_spec(redactor: Redactor, spec: SessionSpec) -> SessionSpec:
    redacted, _ = redactor.redact_model(spec)
    return redacted
