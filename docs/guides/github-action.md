---
description: Gate pull requests on recorded browser-agent runs or coding-agent sessions with the Evidence Judge GitHub Action - inputs, outputs, job summary, exit behaviour and the workflow-command injection guard.
---

# CI with the GitHub Action

The repository is also a composite GitHub Action. It installs Evidence Judge, judges one recorded
run or coding-agent session, writes the verdict to the job summary and fails the step on FAIL.

```yaml title=".github/workflows/agent-evals.yml"
name: agent evals
on: [pull_request]

jobs:
  judge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      # ... run your agent here and record runs/checkout/{trace.zip,run.har,answer.txt}
      - name: Judge the checkout run
        id: judge
        uses: hsayvaidya23/evidence-judge@v0.1.1 # (1)!
        with:
          trace: runs/checkout/trace.zip
          har: runs/checkout/run.har
          answer-file: runs/checkout/answer.txt
          spec: specs/checkout.yaml
          upload-report: "true" # keep the HTML report as an artifact, also on FAIL
          # fail-on-review: "true"
```

1.  Or pin the full commit SHA with a `# v0.1.1` comment, which Dependabot keeps up to date.

!!! warning "Pin a release, not a branch"

    The action installs evidence-judge from the ref you pin, so `@main` would change your
    verdicts whenever `main` moves. Pin a version tag (`@v0.1.1`) or, stricter, its full commit
    SHA (`@<sha> # v0.1.1`).

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `trace` | | Path to the Playwright `trace.zip` |
| `har` | | Path to a HAR recorded alongside the trace |
| `browser-use` | | Path to a browser-use history JSON, or its run directory |
| `answer-file` | | File with the agent's final answer |
| `reasoning-file` | | File with the agent's reasoning, one step per paragraph (steps separated by a blank line) |
| `spec` | | Task spec YAML (required for write tasks to PASS) |
| `site` | | Site the browser run's task is on, passed as `--site`: an http(s) URL or host with an optional port and no path (`shop.example.com`, `https://shop.example.com:8443`). Overrides the spec's `site`; browser runs only |
| `session-transcript` | | A coding-agent session to judge with `evidence-judge session judge` instead of a browser run (see [Judge a coding-agent session](#judge-a-coding-agent-session)) |
| `session-format` | `"claude-code"` | What `session-transcript` is: `claude-code`, `codex` (`exec --json` output or a rollout) or `evidence-log`, in any case |
| `session-spec` | | Session spec YAML for `session-transcript` |
| `offline` | `"true"` | No LLM calls (heuristic claim extraction). `"false"` needs an API key (see [LLM extraction in CI](#llm-extraction-in-ci)) |
| `fail-on-review` | `"false"` | Also fail the job when the verdict is NEEDS_REVIEW |
| `anthropic-api-key` | `""` | Anthropic API key for LLM extraction; pass a secret. Empty keeps `ANTHROPIC_API_KEY` from the job environment |
| `upload-report` | `"false"` | Upload the HTML report as a workflow artifact, also when the step fails |
| `report-artifact` | `"evidence-judge-report"` | Name of that artifact; give each invocation in a job its own |
| `version` | `""` | Git tag, branch or commit SHA of this repository to install (`git+https://github.com/hsayvaidya23/evidence-judge@<version>`); empty installs the action's own source, the ref you put after `@` in `uses:` |
| `python-version` | `"3.13"` | Python version to run with |

At least one of `trace`, `har` or `browser-use` is needed, or `session-transcript`. `offline`, `fail-on-review` and
`upload-report` take `true` or `false` in any case (`"True"`, `"FALSE"`); any other value
(`"yes"`, `"1"`, an empty string) fails the step before anything is installed, instead of being
read as `false`. A `site` with a path, query, user name or a scheme other than http(s) fails the
step the same way; evidence-judge then checks the rest (a public suffix such as `co.uk` is not
a site) and exits 3.

## Outputs

| Output | Value |
| --- | --- |
| `outcome` | `pass`, `fail` or `needs_review` |
| `score` | Score between 0 and 1 |
| `report` | Path to the HTML report (in `$RUNNER_TEMP`) |

## What the step does

1. Checks the boolean, session and `site` inputs, then installs `uv` and evidence-judge with the `anthropic` extra.
2. Runs `evidence-judge judge` (or `evidence-judge session judge` for `session-transcript`)
   with the inputs you gave, writing `verdict.json` and
   `report.html` to a fresh `$RUNNER_TEMP/evidence-judge.XXXXXX` directory, so a second
   invocation in the same job never reads the first one's verdict.
3. Writes the outcome, score and summary to the **job summary** (with
   `python -m evidence_judge.ci_summary`, which escapes the summary text).
4. Exits: `pass` → success; `needs_review` → success, or failure with `fail-on-review: "true"`;
   `fail` → failure. On bad input (exit 3) or an internal error (exit 4), or if no verdict was
   written, the step fails with evidence-judge's exit code.
5. With `upload-report: "true"`, uploads `report.html` as the `report-artifact` artifact,
   whatever the outcome.

## Judge a coding-agent session

With `session-transcript`, the step judges a coding agent's session instead of a browser run:
the agent's final answer ("all tests pass", "I fixed `calc.py`") against the commands, exit
codes, output and edits the session recorded. `answer-file`, `offline`, `fail-on-review`,
`anthropic-api-key` and `upload-report` work as for a browser run; `session-spec` takes a
session spec.

```yaml
      - name: Judge the agent's session
        uses: hsayvaidya23/evidence-judge@v0.1.1
        with:
          session-transcript: codex-run.jsonl # saved with `codex exec --json > codex-run.jsonl`
          session-format: codex
          session-spec: .evidence-judge/session.yaml
          fail-on-review: "true"
```

`session-transcript` cannot be combined with `trace`, `har`, `browser-use`, `reasoning-file`,
`spec` or `site`, `session-spec` needs `session-transcript`, and `session-format` must be one of the three
formats; each of these fails the step before anything is installed. The action never re-runs
the session's tests (`session judge --rerun`): run that as its own step if you need it. A Codex
`exec --json` stream records no diff, so a session that changed code goes to NEEDS_REVIEW (see
the [Codex CLI guide](codex.md)).

## LLM extraction in CI

The action runs offline by default. LLM claim extraction needs an Anthropic API key: pass it
with the `anthropic-api-key` input (or as `ANTHROPIC_API_KEY` in the job or step environment)
and turn offline off:

```yaml
      - uses: hsayvaidya23/evidence-judge@v0.1.1
        env:
          EVJ_MAX_COST_USD: "0.10"
        with:
          trace: runs/checkout/trace.zip
          answer-file: runs/checkout/answer.txt
          spec: specs/checkout.yaml
          offline: "false"
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The input is masked in the log and only set for the judging step; left empty, it does not
override an `ANTHROPIC_API_KEY` the job already sets. With `offline: "false"` and no key, the step
warns and judges offline. Secrets are not passed to workflows triggered from forks, so
pull requests from forks run offline. Any `EVJ_*` setting from
[Configuration](../reference/configuration.md) can be set as an environment variable in the
workflow. A `.env` file in the checked-out repository is never read, so a pull request cannot
change settings such as `EVJ_REDACT` or `EVJ_REVIEW_COVERAGE_THRESHOLD`; when the workflow
itself sets `EVJ_REDACT=false`, the step prints a warning.

## Safe with untrusted agent text

Agent output ends up in the job log, and GitHub runners interpret lines like `::set-output` or
`::add-mask` as workflow commands. The action stops workflow-command processing with a random
`::stop-commands::` token while evidence-judge runs, and resumes it afterwards, so an agent
cannot inject commands through its answer.

The verdict summary quotes the agent too, and job summaries render Markdown and HTML. The action
writes it HTML-escaped inside a `<pre>` block, so links, images, tags, tables and backticks an
agent writes show up as plain text; only the validated outcome and score are written outside
it. The project's CI runs the action (`uses: ./`) on an
honest and a forged demo run and checks its outputs.

!!! note "Artifacts and secrets"

    HTML reports embed screenshots unredacted. Upload them as artifacts only from runs on test
    accounts and synthetic data, and keep real recordings out of the repository; see
    [Security](../security.md).
