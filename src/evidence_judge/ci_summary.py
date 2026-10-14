"""Write a verdict to a GitHub Actions job summary and step outputs, safely.

The verdict summary repeats agent-controlled text, and a job summary renders Markdown and HTML.
The summary is therefore HTML-escaped (``:``, backticks, brackets and newlines included) inside a
single-line ``<pre>`` block, so no link, image, tag, table, heading or ``::`` workflow command an
agent writes can render or run. Only the validated outcome and score appear outside it.

Usage (what the composite action runs)::

    python -m evidence_judge.ci_summary verdict.json [--summary FILE] [--outputs FILE]
        [--report PATH]

``--summary`` and ``--outputs`` default to ``$GITHUB_STEP_SUMMARY`` and ``$GITHUB_OUTPUT``. The
outcome is printed to stdout. Exit code 3 means the verdict could not be read.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

OUTCOMES: Final = frozenset({"pass", "fail", "needs_review"})
_EXTRA_ESCAPES: Final = str.maketrans(
    {":": "&#58;", "`": "&#96;", "[": "&#91;", "]": "&#93;", "\n": "&#10;", "\r": "&#13;"}
)


def _outcome(verdict: Mapping[str, object]) -> str:
    outcome = verdict.get("outcome")
    return outcome if isinstance(outcome, str) and outcome in OUTCOMES else "unknown"


def _score(verdict: Mapping[str, object]) -> float | None:
    score = verdict.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(score):
        return None
    return score


def escape(text: str) -> str:
    """Escape text so a job summary shows it literally inside ``<pre>``."""
    return html.escape(text, quote=True).translate(_EXTRA_ESCAPES)


def render_summary(verdict: Mapping[str, object]) -> str:
    score = _score(verdict)
    summary = verdict.get("summary")
    body = escape(summary if isinstance(summary, str) else "")
    return (
        f"### Evidence Judge: {_outcome(verdict).upper()}\n\n"
        f"Score: {'n/a' if score is None else f'{score:.2f}'}\n\n"
        f"<pre>{body}</pre>\n"
    )


def _outputs(verdict: Mapping[str, object], report: str | None) -> str:
    score = _score(verdict)
    lines = [f"outcome={_outcome(verdict)}", f"score={'' if score is None else score}"]
    if report is not None:
        lines.append(f"report={report}")
    return "".join(f"{line}\n" for line in lines)


def _append(path: str | None, text: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evidence_judge.ci_summary")
    parser.add_argument("verdict")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    parser.add_argument("--outputs", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        loaded: object = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"cannot read verdict {args.verdict}: {exc}", file=sys.stderr)
        return 3
    verdict = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    _append(args.summary, render_summary(verdict))
    _append(args.outputs, _outputs(verdict, args.report))
    print(_outcome(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
