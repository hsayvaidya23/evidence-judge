from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table
from rich.text import Text

from evidence_judge.core.evidence import Channel, Run
from evidence_judge.core.provenance import injections
from evidence_judge.core.verdict import Dimension, Outcome, RunVerdict, Support

_OUTCOME_STYLE = {
    Outcome.PASS: ("PASS", "bold white on green"),
    Outcome.FAIL: ("FAIL", "bold white on red"),
    Outcome.NEEDS_REVIEW: ("NEEDS REVIEW", "bold black on yellow"),
}
_SUPPORT_STYLE = {
    Support.SUPPORTED: ("supported", "green"),
    Support.CONTRADICTED: ("contradicted", "bold red"),
    Support.NO_EVIDENCE: ("no evidence", "yellow"),
}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def print_verdict(verdict: RunVerdict, console: Console) -> None:
    """Human-readable verdict. Agent-controlled strings are rendered as plain text, never markup."""
    label, style = _OUTCOME_STYLE[verdict.outcome]
    head = Text.assemble(
        (f" {label} ", style),
        "  ",
        (f"score {round(verdict.score * 100)}/100", "bold"),
        "  ",
        (verdict.run_id, "dim"),
    )
    console.print(head)
    if verdict.task:
        console.print(Text.assemble(("task ", "dim"), verdict.task))
    console.print(Text(verdict.summary))

    if verdict.claims:
        table = Table(title="Claims", title_justify="left", expand=True, show_lines=False)
        table.add_column("status", no_wrap=True)
        table.add_column("claim", ratio=3)
        table.add_column("reason", ratio=3)
        table.add_column("evidence", ratio=1)
        for cv in verdict.claims:
            text, st = _SUPPORT_STYLE[cv.status]
            claim = Text.assemble(cv.claim_text, "\n", (f'"{cv.quote}"', "dim italic"))
            if cv.key:
                claim = Text.assemble(("[key] ", "bold"), claim)
            table.add_row(
                Text(text, style=st),
                claim,
                Text(cv.reason, style="dim"),
                Text(", ".join(c.evidence_id for c in cv.citations) or "-", style="cyan"),
            )
        console.print(table)
    else:
        console.print(Text("No checkable claims were found.", style="dim"))

    failed = [e for e in verdict.expectations if e.met is not True]
    if failed:
        console.print(Text("Expectations not met", style="bold"))
        for e in failed:
            mark, st = ("x", "red") if e.met is False else ("?", "yellow")
            console.print(
                Text.assemble(
                    (f"  {mark} ", st),
                    (e.expectation_id, "bold"),
                    f" ({e.kind}) ",
                    (e.reason, "dim"),
                )
            )

    findings = [(d, verdict.dimensions[d]) for d in Dimension if d in verdict.dimensions]
    if findings:
        line = Text("Dimensions ", style="bold")
        for d, score in findings:
            line.append(
                f" {d.value} {_pct(score.score)} ", style="dim" if score.score is None else ""
            )
        console.print(line)
        for d, score in findings:
            for f in score.findings:
                console.print(Text.assemble(("  - ", "dim"), (f"{d.value}: ", "bold"), f))

    for w in verdict.warnings:
        console.print(Text.assemble(("warning ", "yellow"), w))

    u = verdict.usage
    console.print(
        Text(
            f"llm calls {u.llm_calls} · tokens {u.input_tokens:,} in / {u.output_tokens:,} out · "
            f"cost ${u.cost_usd:.4f} · coverage {_pct(verdict.claim_coverage)} · "
            f"deterministic {_pct(verdict.deterministic_share)}",
            style="dim",
        )
    )


@dataclass(frozen=True, slots=True)
class Mutation:
    id: str
    method: str
    status: int | None
    url: str


@dataclass(frozen=True, slots=True)
class Injection:
    """A call that made the run's evidence the agent's (core.provenance.injections)."""

    id: str
    call: str
    sha256: str | None


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    run_id: str
    source: str
    captured: list[str]
    counts: dict[str, int]
    mutations: list[Mutation] = field(default_factory=list[Mutation])
    final_url: str | None = None
    final_title: str | None = None
    injections: list[Injection] = field(default_factory=list[Injection])

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "captured": self.captured,
            "counts": self.counts,
            "mutations": [
                {"id": m.id, "method": m.method, "status": m.status, "url": m.url}
                for m in self.mutations
            ],
            "final_url": self.final_url,
            "final_title": self.final_title,
            "injections": [
                {"id": i.id, "call": i.call, "sha256": i.sha256} for i in self.injections
            ],
        }


def summarize_evidence(run: Run) -> EvidenceSummary:
    mutations = [
        Mutation(id=n.id, method=n.method.upper(), status=n.status, url=n.url)
        for n in run.network
        if n.is_mutation
    ]
    return EvidenceSummary(
        run_id=run.run_id,
        source=run.source,
        captured=sorted(c.value for c in run.captured),
        counts={
            Channel.ACTIONS.value: len(run.actions),
            Channel.NETWORK.value: len(run.network),
            "mutations": len(mutations),
            Channel.SNAPSHOTS.value: len(run.snapshots),
            Channel.SCREENSHOTS.value: len(run.screenshots),
            Channel.CONSOLE.value: len(run.console),
        },
        mutations=mutations,
        final_url=run.final_state.url if run.final_state else None,
        final_title=run.final_state.title if run.final_state else None,
        injections=[
            Injection(id=a.id, call=a.value or a.name, sha256=a.script_sha256)
            for a in injections(run)
        ],
    )


def print_evidence_summary(summary: EvidenceSummary, console: Console) -> None:
    console.print(
        Text.assemble(
            ("run ", "dim"), (summary.run_id, "bold"), ("  source ", "dim"), summary.source
        )
    )
    channels = Table(title="Evidence", title_justify="left")
    channels.add_column("channel")
    channels.add_column("items", justify="right")
    channels.add_column("captured")
    for name, n in summary.counts.items():
        captured = name in summary.captured or (
            name == "mutations" and Channel.NETWORK.value in summary.captured
        )
        channels.add_row(name, str(n), Text("yes", "green") if captured else Text("no", "dim"))
    console.print(channels)

    if summary.mutations:
        muts = Table(title="State-changing requests", title_justify="left", expand=True)
        muts.add_column("id", no_wrap=True)
        muts.add_column("method", no_wrap=True)
        muts.add_column("status", no_wrap=True)
        muts.add_column("url", ratio=1, overflow="fold")
        for m in summary.mutations:
            ok = m.status is not None and 200 <= m.status < 400
            muts.add_row(
                m.id,
                m.method,
                Text("-" if m.status is None else str(m.status), "green" if ok else "red"),
                Text(m.url),
            )
        console.print(muts)
    if summary.final_url:
        title = f"  ({summary.final_title})" if summary.final_title else ""
        console.print(Text.assemble(("final ", "dim"), summary.final_url, title))
    if summary.injections:
        made = Table(
            title="Agent-made evidence (list a script's sha256 in the spec's trusted_scripts "
            "to trust it)",
            title_justify="left",
            expand=True,
        )
        made.add_column("id", no_wrap=True)
        made.add_column("call", ratio=1, overflow="fold")
        made.add_column("sha256", no_wrap=True)
        for i in summary.injections:
            made.add_row(i.id, Text(i.call), i.sha256 or Text("-", "dim"))
        console.print(made)
