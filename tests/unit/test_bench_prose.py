"""The benchmark numbers in README.md and docs/index.md match bench/results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.forgery.evidence import EVIDENCE_KINDS

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "bench" / "results" / "offline-fixtures.json"
CONTROL = "style_only"
POOLED = "all manipulations"
TIMES = "\N{MULTIPLICATION SIGN}"

pytestmark = pytest.mark.skipif(not RESULTS.is_file(), reason="the sdist leaves out bench/")


def _bench() -> dict[str, Any]:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def _metric(data: dict[str, Any], judge: str, kind: str) -> dict[str, Any]:
    return next(m for m in data["forgery_metrics"] if m["judge"] == judge and m["kind"] == kind)


def _control(data: dict[str, Any], judge: str) -> dict[str, Any]:
    return next(m for m in data["control_metrics"] if m["judge"] == judge)


def _expected(data: dict[str, Any]) -> dict[str, list[str]]:
    kinds: list[str] = data["kinds"]
    attack = [k for k in kinds if k != CONTROL]
    tampering = [k for k in kinds if k in EVIDENCE_KINDS]
    spec, generic, bare = data["judges"]
    pooled = {j: _metric(data, j, POOLED) for j in data["judges"]}
    control_n = _metric(data, spec, CONTROL)["false_pass"]["n"]
    runs = pooled[spec]["false_pass"]["n"]
    worst = max(p["false_pass"]["k"] for p in pooled.values())
    honest = {j: _control(data, j)["accuracy"] for j in (spec, generic)}
    forged_runs = sum(1 for s in data["scenarios"] if s["forged"])
    readme = [
        f"{worst} of {runs} attack runs passed for every judge",
        f"and {len(tampering)} kinds that also tamper with the evidence",
        f"Counts below pool the {len(attack)} attack kinds ({runs} runs per judge); "
        f"a style-only control adds {control_n} more",
        f"Honest controls: {honest[spec]['k']}/{honest[spec]['n']} correct with the exact spec "
        f"and {honest[generic]['k']}/{honest[generic]['n']} with the generic one",
        *(
            f"| {p['false_pass']['n']} | **{p['false_pass']['k']}** "
            f"({p['needs_review']} sent to review) |"
            for p in pooled.values()
        ),
    ]
    index = [
        f"{pooled[spec]['false_pass']['k']} / {runs}</span>",
        f"{pooled[bare]['false_pass']['k']} / {runs}</span>",
        f"{pooled[bare]['needs_review']} sent to review instead",
        f"{honest[spec]['k']} / {honest[spec]['n']}</span>",
        f"the other {honest[spec]['n'] - honest[spec]['k']} sent to review",
        f"{runs + control_n}</span>",
        f"{forged_runs} failed runs {TIMES} up to {len(kinds)} forgery kinds {TIMES} {data['seeds']} seeds",
    ]
    return {"README.md": readme, "docs/index.md": index}


@pytest.mark.parametrize("page", ["README.md", "docs/index.md"])
def test_benchmark_prose_matches_the_results(page: str) -> None:
    text = (ROOT / page).read_text(encoding="utf-8")
    stale = [phrase for phrase in _expected(_bench())[page] if phrase not in text]
    assert not stale, f"{page} disagrees with {RESULTS.relative_to(ROOT)}; expected {stale}"
