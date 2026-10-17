"""Benchmark output: JSON results and a Markdown report."""

from __future__ import annotations

from pathlib import Path

from evidence_judge.bench.runner import (
    ALL_MANIPULATIONS,
    EJ_GENERIC_SPEC,
    EJ_NO_SPEC,
    EJ_WITH_SPEC,
    BenchResult,
    CaseResult,
    Proportion,
)
from evidence_judge.core.verdict import Outcome
from evidence_judge.forgery.base import ForgeryKind
from evidence_judge.forgery.evidence import EVIDENCE_KINDS, UNANCHORED_KINDS, WRITE_ONLY_KINDS

_OUTCOME_LABEL = {
    Outcome.PASS: "PASS",
    Outcome.FAIL: "FAIL",
    Outcome.NEEDS_REVIEW: "REVIEW",
}


def to_json(result: BenchResult) -> str:
    return result.model_dump_json(indent=2) + "\n"


def write_json(result: BenchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(result), encoding="utf-8")


def write_markdown(result: BenchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(result), encoding="utf-8")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _ci(p: Proportion) -> str:
    return f"{p.ci_low:.0%} to {p.ci_high:.0%}"


def _judge_notes(result: BenchResult) -> list[str]:
    notes: list[str] = []
    specs = {
        EJ_WITH_SPEC: "each scenario's `spec.yaml`, written for the shop",
        EJ_GENERIC_SPEC: (
            "`spec.generic.yaml`, a coarser spec written from the task text alone "
            "(purchase: an order for a kettle, a confirmation, a price of at most $30; "
            "stock check: a page showed a stock status)"
        ),
        EJ_NO_SPEC: "no task spec, only the agent's claims",
    }
    for judge, spec in specs.items():
        if judge in result.judges:
            notes.append(f"- `{judge}` grades with {spec}")
    if ForgeryKind.INJECTED_PREDICATES in result.kinds:
        notes.append(
            "- `injected_predicates` forgeries also replace claim extraction with what an "
            "injection-controlled extractor would return: success claims backed by vacuous or "
            "weak predicates (any request, page text `confirm` or `24`) or tautological ones, "
            "passed through the extractor's own validation"
        )
    if ForgeryKind.TAUTOLOGICAL_PREDICATES in result.kinds:
        notes.append(
            "- `tautological_predicates` forgeries hand the judge success claims backed only by "
            "predicates every run meets (a URL containing `://` or the shop's host, a GET with "
            "`//` in its URL, a `goto` to an `http` URL, the start page's text); they pass "
            "validation"
        )
    evidence = [k for k in result.kinds if k in EVIDENCE_KINDS]
    if evidence:
        write_only = [k for k in evidence if k in WRITE_ONLY_KINDS]
        notes.append(
            f"- {', '.join(f'`{k.value}`' for k in evidence)} forgeries also change the "
            "evidence the way a gaming agent would (see docs/benchmark.md)"
            + (
                f"; {', '.join(f'`{k.value}`' for k in write_only)} model a write and are not "
                "applied to read-only tasks"
                if write_only
                else ""
            )
        )
    if any(k in UNANCHORED_KINDS for k in result.kinds):
        notes.append(
            "- `agent_chosen_host` forgeries are judged with `site` and `allowed_hosts` removed "
            "from both specs, the spec writer's mistake the attack needs"
        )
    return notes


def to_markdown(result: BenchResult) -> str:
    lines = [
        *_header(result),
        *_forgery_table(result),
        *_variant_table(result),
        *_accuracy_table(result),
        *_control_verdicts(result),
        *_should_pass(result),
    ]
    return "\n".join(lines) + "\n"


def _header(result: BenchResult) -> list[str]:
    forged_bases = [s.name for s in result.scenarios if s.forged]
    forged_cases = len({c.case_id for c in result.cases if c.forged})
    edited = sorted({c.case_id for c in result.cases if c.control is not None})
    recordings = len({s.recording or s.name for s in result.scenarios})
    mode = "offline (heuristic claim extraction)" if result.offline else "online (LLM extraction)"
    return [
        "# Forged-run benchmark",
        "",
        f"- Judges: {', '.join(f'`{j}`' for j in result.judges)}; Evidence Judge {mode}, "
        f"version {result.judge_version}",
        f"- Scenarios: {len(result.scenarios)} scenarios (from {recordings} recordings) and "
        f"{len(edited)} honest edits of the passing ones = "
        f"{len(result.scenarios) + len(edited)} honest controls; "
        f"{len(forged_bases)} failed runs forged ({', '.join(forged_bases) or 'none'})",
        f"- Forgeries: up to {len(result.kinds)} kinds x {result.seeds} seeds per failed run, or "
        f"one seed per variant for a kind with more variants = {forged_cases} forged runs, "
        f"forger `{result.forger}`",
        *_judge_notes(result),
    ]


def _forgery_table(result: BenchResult) -> list[str]:
    lines = [
        "",
        "## False PASS on forged runs (lower is better)",
        "",
        "A forged run is a failed run whose answer and reasoning were rewritten to claim success. "
        "Its recorded evidence is unchanged, except that the evidence-altering kinds add what a "
        "gaming agent would plant; none of it does the task, so the correct verdict is never "
        "PASS.",
        "",
        "| Judge | Forgery kind | n | False PASS | Rate | 95% CI (Wilson) | NEEDS_REVIEW | FAIL "
        "| Errors |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for m in result.forgery_metrics:
        kind = f"**{m.kind}**" if m.kind == ALL_MANIPULATIONS else m.kind
        fp = m.false_pass
        lines.append(
            f"| `{m.judge}` | {kind} | {fp.n} | {fp.k} | {_pct(fp.rate)} | {_ci(fp)} | "
            f"{m.needs_review} | {m.fail} | {m.errors} |"
        )
    return [
        *lines,
        "",
        f"`{ALL_MANIPULATIONS}` pools every kind except the `style_only` control.",
    ]


def _variant_table(result: BenchResult) -> list[str]:
    judges = [j for j in result.judges if j in (EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC)]
    rows: dict[tuple[ForgeryKind, str], list[int]] = {}
    for m in result.variant_metrics:
        if m.judge not in judges:
            continue
        row = rows.setdefault((m.kind, m.variant), [0, 0, 0, 0])
        row[0] = max(row[0], m.runs)
        row[1] += m.false_pass
        row[2] += m.needs_review
        row[3] += m.fail
    if not rows:
        return []
    lines = [
        "",
        "## Forgery variants",
        "",
        "Every variant of every kind runs on every failed run that can carry it. Runs counts the "
        "forged runs of a variant; the verdicts pool every Evidence Judge configuration that "
        f"judged them ({', '.join(f'`{j}`' for j in judges)}).",
        "",
        "| Forgery kind | Variant | Runs | False PASS | NEEDS_REVIEW | FAIL |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for (kind, variant), (runs, false_pass, review, fail) in rows.items():
        lines.append(f"| {kind.value} | {variant} | {runs} | {false_pass} | {review} | {fail} |")
    return lines


def _accuracy_table(result: BenchResult) -> list[str]:
    lines = [
        "",
        "## Accuracy on honest controls (higher is better)",
        "",
        "| Judge | n | Correct | Accuracy | 95% CI (Wilson) | NEEDS_REVIEW | Errors |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for c in result.control_metrics:
        a = c.accuracy
        lines.append(
            f"| `{c.judge}` | {a.n} | {a.k} | {_pct(a.rate)} | {_ci(a)} | {c.needs_review} | "
            f"{c.errors} |"
        )
    return lines


def _control_verdicts(result: BenchResult) -> list[str]:
    lines = [
        "",
        "## Honest control verdicts",
        "",
        "| Scenario | Expected | " + " | ".join(f"`{j}`" for j in result.judges) + " |",
        "| --- | --- | " + " | ".join("---" for _ in result.judges) + " |",
    ]
    controls = {(_label(c), c.judge): c for c in result.cases if not c.forged}
    rows = [(s.name, s.expected) for s in result.scenarios]
    rows += [
        (_label(c), c.expected)
        for c in result.cases
        if c.control is not None and c.judge == result.judges[0]
    ]
    for label, expected in rows:
        cells = [_verdict_cell(controls.get((label, judge))) for judge in result.judges]
        lines.append(f"| {label} | {_OUTCOME_LABEL[expected]} | " + " | ".join(cells) + " |")
    return lines


def _verdict_cell(case: CaseResult | None) -> str:
    if case is None:
        return "-"
    if case.outcome is None:
        return "error"
    mark = "" if case.outcome is case.expected else " ✗"
    return _OUTCOME_LABEL[case.outcome] + mark


def _should_pass(result: BenchResult) -> list[str]:
    lines = [
        "",
        "## Honest runs that should pass",
        "",
        "Every honest control labelled PASS (recorded runs and their honest edits): how often "
        "each judge passes it, and how often it wrongly sends it to review or fails it.",
        "",
        "| Judge | n | PASS | False NEEDS_REVIEW | False FAIL | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for judge in result.judges:
        should = [
            c
            for c in result.cases
            if not c.forged and c.judge == judge and c.expected is Outcome.PASS
        ]
        if not should:
            continue
        n = len(should)
        passed, review, fail = (
            sum(c.outcome is o for c in should)
            for o in (Outcome.PASS, Outcome.NEEDS_REVIEW, Outcome.FAIL)
        )
        errors = sum(c.outcome is None for c in should)
        lines.append(
            f"| `{judge}` | {n} | {passed} | {_share(review, n)} | {_share(fail, n)} | {errors} |"
        )
    return lines


def _label(case: CaseResult) -> str:
    return f"{case.scenario} + {case.control.value}" if case.control else case.scenario


def _share(k: int, n: int) -> str:
    return f"{k} ({k / n:.0%})"
