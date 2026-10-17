---
description: Use Evidence Judge from Python - load a browser run or a coding-agent session, redact it, judge it with or without a spec, and write JSON and HTML reports.
---

# Python API

Everything the CLI does is available from Python. The judge is async and stateless between
runs, so one `Judge` can grade many runs. Import from the package root: those names are the
supported API (see [Stability](#stability)).

## Judge a run

```python
import asyncio

from evidence_judge import Judge, JudgeOptions, Settings, TaskSpec, build_llm, load_run

settings = Settings()  # (1)!
run = load_run(trace="trace.zip", har="run.har", answer_file="answer.txt")  # (2)!
judge = Judge(settings, build_llm(settings))  # (3)!
verdict = asyncio.run(judge.judge(run, JudgeOptions(spec=TaskSpec.load("spec.yaml"))))
print(verdict.outcome, verdict.summary)
```

1.  Reads `EVJ_*` environment variables only; no `.env` file is read.
2.  Any of `trace`, `har` and `browser_use`, plus `answer` or `answer_file`, `reasoning_file`,
    `task`, `run_id`, `max_body_chars`, `allow_mismatch` and `screenshot_root`.
3.  `build_llm` returns `None` when no API key is configured; the judge then runs offline.

!!! note "Redaction"

    `Judge.judge` redacts the run, spec and claims before claim extraction and any LLM call
    whenever `settings.redact` is on (`EVJ_REDACT`, default true), exactly as the CLI does: the
    CLI goes through the same `Judge.redact`. To write a report, redact first so the report shows
    what was judged; the returned options are marked so nothing is redacted twice:

    ```python
    run, options, report = judge.redact(run, JudgeOptions(spec=TaskSpec.load("spec.yaml")))
    verdict = asyncio.run(judge.judge(run, options))
    ```

    Pass `Judge(..., redactor=Redactor(key))` to choose the key; by default it is
    `EVJ_REDACTION_KEY`, else the key file in `EVJ_CACHE_DIR` when the judge has an LLM, else a
    random key per process.

## Options

```python
JudgeOptions(
    offline=False,  # True: heuristic extraction, no model calls
    spec=None,  # a TaskSpec
    claims=None,  # a pre-extracted ClaimSet; skips extraction
    redact=True,  # False: the inputs are already redacted (Judge.redact returns this)
)
```

## Judge a coding-agent session

```python
import asyncio
from pathlib import Path

from evidence_judge import (
    SessionJudge,
    SessionJudgeOptions,
    SessionSpec,
    Settings,
    build_llm,
    load_session,
)

settings = Settings()
run = load_session(claude_code=Path("session.jsonl"))  # (1)!
judge = SessionJudge(settings, build_llm(settings))  # (2)!
options = SessionJudgeOptions(spec=SessionSpec.load(Path("session-spec.yaml")))
verdict = asyncio.run(judge.judge(run, options))
print(verdict.outcome, verdict.verification)
```

1.  One of `claude_code` (a transcript `.jsonl` path), `codex` (`exec --json` output or a
    rollout) and `evidence_log` (with `log_key` and `log_anchor`), plus `answer` to judge
    another final answer. An unreadable file raises `UnrecognisedSessionError` (a
    `ValueError`), saying what the file looks like.
2.  The verdict is the same `RunVerdict` the browser judge returns, with `verification`
    saying whether test results came from the transcript or a re-run.

`SessionJudge.judge` redacts the session (commands, outputs, task and answer), spec and claims
before claim extraction whenever `settings.redact` is on, exactly as `session judge` does:
the CLI goes through the same `SessionJudge.redact`, which returns options marked
`redact=False`. Pass `SessionJudge(..., redactor=Redactor(key))` to choose the key.

```python
SessionJudgeOptions(
    offline=False,  # True: heuristic extraction, no model calls
    spec=None,  # a SessionSpec
    claims=None,  # a pre-extracted SessionClaimSet; skips extraction
    require_tamper_evidence=False,  # cap at needs_review unless an evidence log or harness stream
    run_id=None,  # default: the session id
    trusted_log=False,  # the evidence log, key and anchor were out of the agent's reach
    rerun=None,  # a re-run result that decides test results (evidence_judge.sessions.rerun)
    require_rerun=False,  # refuse a pass unless a re-run decided the suite
    redact=True,  # False: the inputs are already redacted (SessionJudge.redact returns this)
)
```

## Reports

```python
from pathlib import Path

from evidence_judge.report.html_report import write_html
from evidence_judge.report.json_report import verdict_to_json, write_json

write_json(verdict, Path("verdict.json"))
write_html(verdict, run, Path("report.html"))  # needs the run for the timeline and screenshots
print(verdict_to_json(verdict))
```

The HTML report reads screenshots lazily from the trace, so keep the trace file in place until
the report is written.

A session verdict's HTML report takes the session instead of a browser run (`judge` is a
`SessionJudge`):

```python
from evidence_judge.sessions.html import write_session_html

run, options, _ = judge.redact(run, options)  # the report shows the redacted session
verdict = asyncio.run(judge.judge(run, options))
write_session_html(verdict, run, Path("session-report.html"))
```

## Loading evidence

| Function | Module | Reads |
| --- | --- | --- |
| `load_run(...)` | `evidence_judge.ingest` | Any combination of sources, merged into one `Run` |
| `load_playwright_trace(path, limits=TraceLimits(...))` | `evidence_judge.ingest` | A trace zip, with size limits |
| `load_har(path)` | `evidence_judge.ingest` | A HAR file |
| `load_browser_use_history(path, screenshot_root=...)` | `evidence_judge.ingest` | A browser-use history or run directory |
| `attach_har(trace_run, har_run, allow_mismatch=False)` | `evidence_judge.ingest` | Merge a HAR into a trace run |

Every ingest failure is an `IngestError` (a `ValueError`): `TraceFormatError`,
`UnsupportedTraceVersionError`, `HarFormatError`, `BrowserUseFormatError`,
`MismatchedEvidenceError`, and `TooLargeError` / `TraceTooLargeError`.

## Key types

| Type | Module | What |
| --- | --- | --- |
| `Run` | `evidence_judge.core.evidence` | The normalised recording; `captured` lists the channels |
| `Claim`, `ClaimSet` | `evidence_judge.core.claims` | What the agent asserted, with verbatim quotes |
| `Predicate` | `evidence_judge.core.predicates` | Discriminated union of the six predicate kinds |
| `TaskSpec` | `evidence_judge.core.taskspec` | What a successful run must contain; `TaskSpec.load(path)` |
| `RunVerdict` | `evidence_judge.core.verdict` | The output ([schema](verdict-schema.md)) |
| `EvidenceIndex` | `evidence_judge.core.index` | The only way checkers read evidence |

## Integrations

```python
from evidence_judge.integrations.otel import emit_verdict
from evidence_judge.integrations.langfuse import push_to_langfuse
from evidence_judge.integrations.phoenix import push_to_phoenix
from evidence_judge.integrations.inspect_ai import evidence_judge_scorer
```

Each needs its extra; details in [Integrations](../integrations.md).

## Benchmark and forgery

```python
from evidence_judge.bench import BenchConfig, BenchmarkRunner, load_scenarios, to_markdown
from evidence_judge.forgery import ForgeryKind, forge
```

See [Benchmark your own agent](../guides/benchmark-your-agent.md).

## Stability

Evidence Judge follows [Semantic Versioning](https://semver.org/). While the version is 0.x, a
minor release (0.1 → 0.2) may break what SemVer covers, and the [changelog](../changelog.md)
says how; a patch release never does. From 1.0, only a major release may.

SemVer covers:

- **The CLI**: commands, flags, environment variables and exit codes (`0` pass, `1` fail,
  `2` needs review, `3` bad input, `4` internal or LLM error).
- **The verdict JSON**: the format named by its `schema_id`
  (`evidence-judge/run-verdict@1`, see [Verdict JSON schema](verdict-schema.md)). An
  incompatible change bumps the number after `@`; new optional fields may appear without one.
- **The Python names exported from `evidence_judge`** (its `__all__`):

| Name | What |
| --- | --- |
| `Judge`, `JudgeOptions` | Grade a run, with or without a spec |
| `load_run` | Load a trace, HAR or browser-use history into a run |
| `TaskSpec` | A task spec; `TaskSpec.load(path)` |
| `SessionJudge`, `SessionJudgeOptions` | Grade a coding-agent session, with or without a session spec |
| `load_session` | Load a Claude Code transcript, Codex stream or rollout, or an evidence log into a session |
| `SessionSpec` | A session spec; `SessionSpec.load(path)` |
| `RunVerdict`, `Outcome` | The verdict and its `pass` / `fail` / `needs_review` outcome |
| `Settings` | Configuration from `EVJ_*` environment variables |
| `build_llm` | The configured LLM client, or `None` offline |
| `Redactor` | The redaction `Judge` and `SessionJudge` apply; pass one to either to choose its key |
| `__version__` | The installed version |

Everything else, including every module path under `evidence_judge.` (`evidence_judge.core`,
`evidence_judge.ingest`, `evidence_judge.report`, the integrations, bench and forgery modules
above), is internal: it works today and the examples on this page use it, but it may change in
any 0.x release without notice.
