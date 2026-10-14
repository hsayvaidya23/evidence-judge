"""Benchmark output: JSON results and a Markdown report."""

from __future__ import annotations

from pathlib import Path

from evidence_judge.bench.runner import (
    ALL_MANIPULATIONS,
    EJ_GENERIC_SPEC,
    EJ_NO_SPEC,
    EJ_WITH_SPEC,
    BenchResult,
    Proportion,
)
from evidence_judge.core.verdict import Outcome
from evidence_judge.forgery.base import ForgeryKind

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
            "(an order for a kettle, a confirmation, a price of at most $30)"
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
            "weak predicates (any request, page text `confirm` or `24`), passed through the "
            "extractor's own validation"
        )
    return notes


def to_markdown(result: BenchResult) -> str:
    forged_bases = [s.name for s in result.scenarios if s.forged]
    forged_cases = len({c.case_id for c in result.cases if c.forged})
    mode = "offline (heuristic claim extraction)" if result.offline else "online (LLM extraction)"
    lines = [
        "# Forged-run benchmark",
        "",
        f"- Judges: {', '.join(f'`{j}`' for j in result.judges)}; Evidence Judge {mode}, "
        f"version {result.judge_version}",
        f"- Scenarios: {len(result.scenarios)} honest controls; "
        f"{len(forged_bases)} failed runs forged ({', '.join(forged_bases) or 'none'})",
        f"- Forgeries: {len(result.kinds)} kinds x {result.seeds} seeds per failed run = "
        f"{forged_cases} forged runs, forger `{result.forger}`",
        *_judge_notes(result),
        "",
        "## False PASS on forged runs (lower is better)",
        "",
        "A forged run is a failed run whose answer and reasoning were rewritten to claim success; "
        "its actions and network evidence are unchanged, so the correct verdict is never PASS.",
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
    lines += [
        "",
        f"`{ALL_MANIPULATIONS}` pools every kind except the `style_only` control.",
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
    lines += [
        "",
        "## Honest control verdicts",
        "",
        "| Scenario | Expected | " + " | ".join(f"`{j}`" for j in result.judges) + " |",
        "| --- | --- | " + " | ".join("---" for _ in result.judges) + " |",
    ]
    controls = {(c.scenario, c.judge): c for c in result.cases if not c.forged}
    for s in result.scenarios:
        cells: list[str] = []
        for judge in result.judges:
            case = controls.get((s.name, judge))
            if case is None:
                cells.append("-")
            elif case.outcome is None:
                cells.append("error")
            else:
                mark = "" if case.outcome is case.expected else " ✗"
                cells.append(_OUTCOME_LABEL[case.outcome] + mark)
        lines.append(f"| {s.name} | {_OUTCOME_LABEL[s.expected]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
