# CLI reference

```text
evidence-judge [OPTIONS] COMMAND [ARGS]...
```

| Command | What it does |
| --- | --- |
| `judge` | Judge one agent run |
| `inspect` | Summarise the evidence in a trace, HAR or browser-use run without judging it |
| `schema` | Print the JSON Schema of the verdict format |
| `version` | Print the evidence-judge version |

Settings (models, budget, redaction, cache) come from environment variables or a `.env` file;
see the configuration table in the [README](../README.md#configuration).

Exit codes are the same for every command: `0` pass, `1` fail, `2` needs review, `3` bad input
(missing or unreadable evidence, an invalid spec, claims file or setting, a corrupt redaction key
file), `4` internal or LLM error. `inspect`, `schema` and `version` exit `0` on success.

## `evidence-judge judge`

```text
evidence-judge judge [--trace FILE] [--har FILE] [--browser-use PATH] [OPTIONS]
```

At least one of `--trace`, `--har` or `--browser-use` is required.

### Evidence

| Option | Description |
| --- | --- |
| `--trace FILE` | Playwright trace `.zip` (format v7 to v10) |
| `--har FILE` | HAR 1.2 file. With `--trace`, fills in bodies and statuses the trace missed and adds requests it did not see; what only the HAR holds can contradict but never prove, and makes `tamper_detection` `partial` |
| `--browser-use PATH` | browser-use `AgentHistoryList` JSON, or a run directory containing `history.json` |
| `--allow-mismatch` | Merge a HAR even when it does not look like a recording of the trace's run. By default the load fails (exit 3) when a HAR shares none of the trace's requests, or under half of them for traces with 5 or more requests, and HAR-only requests more than 10 minutes outside the trace are dropped |
| `--screenshot-root PATH` | Directory browser-use screenshots may be read from. Default: the history file's directory. Paths resolving outside it (symlinks included) are skipped |

### What the agent said

| Option | Description |
| --- | --- |
| `--task TEXT` | Task given to the agent. Default: the spec's `task` |
| `--answer TEXT` | The agent's final answer |
| `--answer-file FILE` | File with the agent's final answer (exclusive with `--answer`) |
| `--reasoning-file FILE` | File with the agent's reasoning, one step per blank-line separated paragraph |
| `--claims FILE` | Pre-extracted `ClaimSet` JSON; skips claim extraction |
| `--run-id TEXT` | Override the run id |

### What success means

| Option | Description |
| --- | --- |
| `--spec FILE` | Task spec YAML ([reference](task-specs.md)). Without a spec, any state-changing request is a side effect that sends the run to review, so a write task (ordering, booking, submitting) can only PASS with a spec that expects or allows its requests |
| `--require-tamper-evidence` | Cap the run at `NEEDS_REVIEW` unless its evidence can fully show tampering (`tamper_detection.level` is `full`: a Playwright trace, with its call log). HAR-only and browser-use runs then cannot pass, and neither can a trace whose `--har` added requests or filled fields. The spec's `require_tamper_evidence: true` does the same. Use it when the verdict gates CI and the trace is recorded by the harness, not the agent |
| `--site URL` | The site the task is on, as a URL or host name with an optional port (`https://shop.example.com`, `shop.example.com`, `127.0.0.1:8000`); overrides the spec's `site`. A site is a host and a port (80 and 443 are the default): only evidence from this host, with or without `www.`, on this port, and from the spec's `allowed_hosts` can prove a claim or an `expect` entry; subdomains count only when `allowed_hosts` lists them. With a site named, a request to another host or port proves nothing. Public suffixes (`com`, `co.uk`, `myshopify.com`), single-label names other than `localhost`, wildcards, user info and a path, query or fragment (`github.com/myorg`: a site is a whole host) are refused (exit code 3). A run can `PASS` only with a named site: `--site`, or the spec's `site` or `allowed_hosts`. Without any of them, the site is inferred from the run's first page, which the agent may have chosen, so no run passes: it gets `NEEDS_REVIEW` instead, with a spec or without one |

### Output and behaviour

| Option | Description |
| --- | --- |
| `--out FILE` | Write the verdict JSON here (schema: `evidence-judge schema`) |
| `--html FILE` | Write the self-contained HTML report here. Screenshots are embedded unredacted |
| `--offline` / `--no-offline` | No LLM calls; heuristic claim extraction and no screenshot checks. Also never reads or creates the redaction key file. Default: off, but runs offline anyway when no `ANTHROPIC_API_KEY` is set |
| `--no-redact` | Keep secrets and PII in evidence, prompts and reports. Use only on synthetic data |
| `--quiet`, `-q` | Print nothing; only set the exit code |
| `--debug` / `--no-debug` | Re-raise internal errors with a traceback instead of exiting `4` |

### Export to other tools

| Option | Description |
| --- | --- |
| `--otel` | Emit `gen_ai.evaluation.result` events through OpenTelemetry, configured by the standard `OTEL_*` variables (and `TRACEPARENT` to attach to the judged span). Needs the `otel` extra; a missing extra exits `3` before judging |
| `--langfuse-trace-id ID` | Push the overall score, outcome and dimension scores to this Langfuse trace, using `LANGFUSE_*` variables. Needs the `langfuse` extra |

A failed export prints a warning and never changes the exit code. See [integrations.md](integrations.md).

Messages on stderr: the redaction summary, `no LLM configured; judging offline`, a warning when
the redaction key file cannot be written (a per-process key is used instead), and a notice when
`EVJ_SCREENSHOTS_TO_LLM=true` lets unredacted screenshots reach the model provider.

## `evidence-judge inspect`

```text
evidence-judge inspect [--trace FILE] [--har FILE] [--browser-use PATH] [--json] [--no-redact]
```

Shows what was captured (channels, request counts, state-changing requests, final URL) so you can
check a recording before judging it. At least one evidence option is required. Output is redacted
with a per-process key; `inspect` never reads or creates the redaction key file.

| Option | Description |
| --- | --- |
| `--trace FILE` | Playwright trace `.zip` |
| `--har FILE` | HAR file (merged with `--trace` like `judge` does) |
| `--browser-use PATH` | browser-use history JSON or run directory |
| `--json` | Print machine-readable JSON instead of a table |
| `--no-redact` | Show raw URLs and values |
| `--debug` / `--no-debug` | Re-raise internal errors with a traceback instead of exiting `4` |

## `evidence-judge bench`

Benchmarks judges on forged runs: false-PASS rates and accuracy on honest controls. See
[benchmark.md](benchmark.md).

```bash
evidence-judge bench RUNS_DIR [--seeds 3] [--out results.json] [--markdown results.md]
```

| Option | Description |
| --- | --- |
| `RUNS_DIR` | Directory of labelled runs, one sub-directory per scenario (`trace.zip`, optional `run.har`, `answer.txt`, `reasoning.txt`, `spec.yaml`, `expected.json`) |
| `--seeds N` | Forgeries per failed run and forgery kind (default 3) |
| `--out FILE` | Write the results JSON here |
| `--markdown FILE` | Write the Markdown report here |
| `--with-llm-baseline` | Also judge every case with a transcript LLM judge (needs `ANTHROPIC_API_KEY`) |
| `--baseline-screenshot` | Show the baseline judge the final screenshot |
| `--offline` / `--online` | Evidence Judge without (default) or with LLM claim extraction |
| `--no-redact` | Judge unredacted evidence and agent text |
| `--quiet`, `-q` | Do not print the report |
| `--debug` / `--no-debug` | Show tracebacks for internal errors |

## `evidence-judge schema`

| Option | Description |
| --- | --- |
| `--out FILE` | Write the verdict JSON Schema to this file instead of stdout |

## `evidence-judge version`

Prints `evidence-judge <version>`.
