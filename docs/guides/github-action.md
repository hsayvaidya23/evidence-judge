---
description: Gate pull requests on recorded agent runs with the Evidence Judge GitHub Action - inputs, outputs, job summary, exit behaviour and the workflow-command injection guard.
---

# CI with the GitHub Action

The repository is also a composite GitHub Action. It installs Evidence Judge, judges one recorded
run, writes the verdict to the job summary and fails the step on FAIL.

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
        uses: hsayvaidya23/evidence-judge@main
        with:
          trace: runs/checkout/trace.zip
          har: runs/checkout/run.har
          answer-file: runs/checkout/answer.txt
          spec: specs/checkout.yaml
          # fail-on-review: "true"
      - name: Keep the HTML report
        if: always() && steps.judge.outputs.report != ''
        uses: actions/upload-artifact@v7
        with:
          name: evidence-judge-report
          path: ${{ steps.judge.outputs.report }}
```

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `trace` | | Path to the Playwright `trace.zip` |
| `har` | | Path to a HAR recorded alongside the trace |
| `browser-use` | | Path to a browser-use history JSON, or its run directory |
| `answer-file` | | File with the agent's final answer |
| `reasoning-file` | | File with the agent's reasoning, one step per paragraph (steps separated by a blank line) |
| `spec` | | Task spec YAML (required for write tasks to PASS) |
| `offline` | `"true"` | No LLM calls (heuristic claim extraction) |
| `fail-on-review` | `"false"` | Also fail the job when the verdict is NEEDS_REVIEW |
| `version` | `""` | Git tag, branch or commit SHA of this repository to install (`git+https://github.com/hsayvaidya23/evidence-judge@<version>`); empty installs the action's own source, the ref you put after `@` in `uses:` |
| `python-version` | `"3.13"` | Python version to run with |

At least one of `trace`, `har` or `browser-use` is needed.

## Outputs

| Output | Value |
| --- | --- |
| `outcome` | `pass`, `fail` or `needs_review` |
| `score` | Score between 0 and 1 |
| `report` | Path to the HTML report (in `$RUNNER_TEMP`) |

## What the step does

1. Installs `uv` and then evidence-judge with the `anthropic` extra.
2. Runs `evidence-judge judge` with the inputs you gave, writing `verdict.json` and
   `report.html` to a fresh `$RUNNER_TEMP/evidence-judge.XXXXXX` directory, so a second
   invocation in the same job never reads the first one's verdict.
3. Writes the outcome, score and summary to the **job summary** (with
   `python -m evidence_judge.ci_summary`, which escapes the summary text).
4. Exits: `pass` → success; `needs_review` → success, or failure with `fail-on-review: "true"`;
   `fail` → failure. On bad input (exit 3) or an internal error (exit 4), or if no verdict was
   written, the step fails with evidence-judge's exit code.

## LLM extraction in CI

The action runs offline by default. To use LLM claim extraction, pass the key as an environment
variable and turn offline off:

```yaml
      - uses: hsayvaidya23/evidence-judge@main
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          EVJ_MAX_COST_USD: "0.10"
        with:
          trace: runs/checkout/trace.zip
          answer-file: runs/checkout/answer.txt
          spec: specs/checkout.yaml
          offline: "false"
```

Any `EVJ_*` setting from [Configuration](../reference/configuration.md) can be set the same way.

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
