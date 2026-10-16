"""Running the benchmark and rendering its results."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from evidence_judge.core.verdict import Outcome
from evidence_judge.sessions.bench.cases import cases
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions


@dataclass(slots=True)
class KindResult:
    n: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])

    @property
    def false_pass(self) -> int:
        return self.outcomes[Outcome.PASS.value]


@dataclass(slots=True)
class BenchResult:
    forgeries: dict[str, KindResult] = field(default_factory=dict[str, KindResult])
    controls: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    @property
    def forged(self) -> int:
        return sum(k.n for k in self.forgeries.values())

    @property
    def false_passes(self) -> int:
        return sum(k.false_pass for k in self.forgeries.values())

    @property
    def control_errors(self) -> int:
        return sum(expected != got for _, expected, got in self.controls)

    def to_dict(self) -> dict[str, object]:
        return {
            "forged": self.forged,
            "false_passes": self.false_passes,
            "kinds": {k: {"n": v.n, **dict(v.outcomes)} for k, v in sorted(self.forgeries.items())},
            "controls": [
                {"session": name, "expected": expected, "outcome": got}
                for name, expected, got in self.controls
            ],
        }


async def run_benchmark(root: Path) -> BenchResult:
    judge = SessionJudge()
    result = BenchResult()
    for case in cases(root):
        verdict = await judge.judge(case.run, SessionJudgeOptions(offline=True, spec=case.spec))
        if case.expected is not None:
            result.controls.append((case.name, case.expected.value, verdict.outcome.value))
            continue
        kind = result.forgeries.setdefault(case.kind, KindResult())
        kind.n += 1
        kind.outcomes[verdict.outcome.value] += 1
    return result


def _cell(text: str) -> str:
    """`text` as one Markdown table cell: a pipe would end the cell and a newline the row."""
    return text.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def to_markdown(result: BenchResult) -> str:
    lines = [
        "| Forgery | n | false PASS | fail | needs review |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for kind, counts in sorted(result.forgeries.items()):
        lines.append(
            f"| {kind} | {counts.n} | {counts.false_pass} | {counts.outcomes['fail']} | "
            f"{counts.outcomes['needs_review']} |"
        )
    lines.append(
        f"| **all** | **{result.forged}** | **{result.false_passes}** | "
        f"{sum(k.outcomes['fail'] for k in result.forgeries.values())} | "
        f"{sum(k.outcomes['needs_review'] for k in result.forgeries.values())} |"
    )
    lines += ["", "| Honest session | expected | outcome |", "| --- | --- | --- |"]
    lines += [f"| {_cell(name)} | {expected} | {got} |" for name, expected, got in result.controls]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Forgery benchmark for the session judge.")
    parser.add_argument("fixtures", type=Path, help="tests/fixtures/sessions")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--out", type=Path, help="Also write the results JSON here")
    parser.add_argument("--markdown", type=Path, help="Also write the Markdown report here")
    args = parser.parse_args()
    result = asyncio.run(run_benchmark(args.fixtures))
    as_json = json.dumps(result.to_dict(), indent=2)
    if args.out:
        args.out.write_text(as_json + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(to_markdown(result), encoding="utf-8")
    print(as_json if args.json else to_markdown(result), end="")
