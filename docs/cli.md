# CLI reference

```text
evidence-judge [OPTIONS] COMMAND [ARGS]...
```

| Command | What it does |
| --- | --- |
| `judge` | Judge one agent run |
| `inspect` | Summarise the evidence in a trace, HAR or browser-use run without judging it |
| `mcp` | Run an MCP server so coding agents can judge, inspect and explain runs as tools |
| `session judge` | Judge a coding-agent session (Claude Code, Codex) against its tool-call log |
| `session inspect` | Summarise a coding-agent session without judging it |
| `session rerun` | Re-run a spec's test command on a copy of the project and judge the runner's own report |
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
| `--offline` / `--no-offline` | No LLM calls; heuristic claim extraction and no screenshot checks. Also never reads or creates the redaction key file. Default: off, but runs offline anyway on the `anthropic` backend when no `ANTHROPIC_API_KEY` is set |
| `--llm BACKEND` | Where LLM calls go for this run: `anthropic` (the API, with `ANTHROPIC_API_KEY`), or `claude-code`, `codex` or `gemini` to run that CLI headless on your existing login, with its tools off. Default: `EVJ_LLM_BACKEND`. A CLI that is missing or not logged in exits `4`. `--offline` wins over it |
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
| `--with-llm-baseline` | Also judge every case with a transcript LLM judge (needs an LLM: `ANTHROPIC_API_KEY` or `--llm`) |
| `--baseline-screenshot` | Show the baseline judge the final screenshot (not with `--llm gemini`, which cannot send images) |
| `--offline` / `--online` | Evidence Judge without (default) or with LLM claim extraction |
| `--llm BACKEND` | The LLM backend for `--online` and `--with-llm-baseline`: `anthropic`, `claude-code`, `codex` or `gemini`. Default: `EVJ_LLM_BACKEND` |
| `--no-redact` | Judge unredacted evidence and agent text |
| `--quiet`, `-q` | Do not print the report |
| `--debug` / `--no-debug` | Show tracebacks for internal errors |

## `evidence-judge mcp`

Runs an MCP server exposing `judge_run`, `inspect_run`, `explain_verdict`, `validate_spec` and
`find_recordings`. Needs the `mcp` extra (`pip install "evidence-judge[mcp]"`); without it the
command exits `3`. See the [MCP server guide](guides/mcp.md) for client setup.

```bash
evidence-judge mcp [--root DIR]... [--http] [--port 8000] [--offline-only]
```

| Option | Description |
| --- | --- |
| `--root DIR` | Directory clients may read and write files in; repeat for more. Paths resolve with every symlink followed, and anything outside every root is refused. Relative paths in tool calls resolve against the first root. Default: the working directory |
| `--http` | Serve streamable HTTP at `http://127.0.0.1:<port>/mcp` instead of stdio. Binds to loopback only, with DNS-rebinding protection and no authentication |
| `--port N` | Port for `--http` (default 8000) |
| `--offline-only` | Refuse `judge_run` calls with `offline: false`, so the server never calls a model even when `ANTHROPIC_API_KEY` is set |

Settings come from the server's environment as for every command; `EVJ_REDACT` decides whether
tool results are redacted.

## `evidence-judge session`

Grades coding-agent sessions (Claude Code, Codex CLI) by checking the agent's final answer
against the session's tool-call log: the commands it ran with their exit status and output, and
the files it edited. See [Coding-agent sessions](design/coding-agent-sessions.md) for the
formats, predicates and soundness rules.

### `evidence-judge session judge`

```text
evidence-judge session judge [--claude-code SESSION] [--codex FILE] [--evidence-log FILE] [OPTIONS]
```

At least one of `--claude-code`, `--codex` or `--evidence-log` is required. With an evidence
log, tool calls come from the log, and a transcript given alongside supplies only the task and
the final answer when the log lacks them.

| Option | Description |
| --- | --- |
| `--claude-code SESSION` | Claude Code transcript: a `.jsonl` path, a session id (looked up under `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`), or `latest` for the newest transcript of `--project` |
| `--project PATH` | Project directory: where `--claude-code latest` looks, and the tree `--rerun` copies (default: the current directory) |
| `--codex FILE` | Codex `exec --json` output saved by the harness, or a rollout file from `~/.codex/sessions` |
| `--evidence-log FILE` | Evidence-recorder log (format version 1), written by a harness hook after every tool call; the only transcript-independent source. Its `tamper_detection` is `full` only when its HMAC chain and head anchor verify **and** `--trusted-log` is given; otherwise `partial`, with the reason |
| `--log-key FILE` | The evidence-log key (default: `log-key` in the hooks' state directory, `EVJ_HOOK_STATE_DIR` or `~/.evidence-judge/state`) |
| `--log-anchor FILE` | The evidence log's head anchor (default: `<state>/<session>/log-head.json`) |
| `--trusted-log` | Assert that the log, its key and anchor were out of the agent's reach (a sandbox that denies the state directory, a container, another OS user). Without it, an agent running as your user could have read the key and signed a forged log, so a verified log is still `partial`. The spec's `trusted_evidence_log: true` does the same |
| `--answer TEXT` / `--answer-file FILE` | Judge this final answer instead of the recorded one |
| `--spec FILE` | Session spec YAML: `task`, `expect` and `forbid` lists of predicates, `ignore_edits` globs, `require_tamper_evidence`, a `rerun` block |
| `--claims FILE` | Pre-extracted session claims JSON; skips extraction |
| `--out FILE` | Write the verdict JSON (schema `evidence-judge/run-verdict@1`) here |
| `--offline` / `--no-offline` | No LLM calls; heuristic claim extraction only |
| `--no-redact` | Keep secrets in commands, outputs and reports |
| `--require-tamper-evidence` | Cap the run at needs review unless it comes from an evidence log or a harness stream |
| `--rerun` | Run the spec's `rerun` command on a copy of `--project` in a clean environment and decide `test_result` predicates from the runner's own report instead of the recorded output; see [Independent re-run](design/coding-agent-sessions.md#independent-re-run) |
| `--require-rerun` | Refuse a pass unless a re-run decided the suite's result |
| `--rerun-base REV` | Git revision the runner configuration is compared with (default: the spec's `rerun.base`, else `HEAD`) |
| `--rerun-baseline FILE` | Fingerprint of `.venv`, `venv` and `node_modules` saved when the turn started (the turn-start hooks write it); the re-run needs review when they changed since, or when the file is missing or unreadable |
| `--run-id TEXT` | Override the run id (default: the session id) |
| `--quiet`, `-q` | Only set the exit code |
| `--debug` / `--no-debug` | Show tracebacks for internal errors |

### `evidence-judge session rerun`

```text
evidence-judge session rerun --spec FILE [--project PATH] [--out FILE]
```

Runs the spec's `rerun` command once, on a copy of the project, in an environment built from
scratch, and judges the runner's machine-readable report, without a session record: pass when
the report shows every test passing and the command exited 0, fail when it shows a failure,
needs review when there is no usable report, the run timed out, or runner configuration differs
from the base revision. The verdict's `verification.method` is `rerun`.

| Option | Description |
| --- | --- |
| `--spec FILE` | Session spec YAML with a `rerun` block |
| `--project PATH` | The project to copy (default: the current directory) |
| `--rerun-base REV` | As for `session judge` |
| `--rerun-baseline FILE` | As for `session judge` |
| `--out FILE` | Write the verdict JSON here |
| `--quiet`, `-q` | Only set the exit code |
| `--debug` / `--no-debug` | Show tracebacks for internal errors |

### `evidence-judge session inspect`

```text
evidence-judge session inspect [--claude-code SESSION] [--codex FILE] [--evidence-log FILE] [--json]
```

Lists every recorded command and edit with what the judge makes of it: the test runner and its
summary, filters, masked exit statuses, summary text typed by the agent, possible workspace
changes and test-weakening edits, plus the last change that later test runs must follow.

| Option | Description |
| --- | --- |
| `--claude-code SESSION`, `--project PATH`, `--codex FILE`, `--evidence-log FILE` | As for `session judge` |
| `--json` | Print machine-readable JSON instead of a table |
| `--no-redact` | Show commands and outputs unredacted |
| `--debug` / `--no-debug` | Re-raise internal errors with a traceback instead of exiting `4` |

## `evidence-judge schema`

| Option | Description |
| --- | --- |
| `--out FILE` | Write the verdict JSON Schema to this file instead of stdout |

## `evidence-judge version`

Prints `evidence-judge <version>`.

## `evidence-judge hook`

Entry points coding agents run themselves. Each reads the agent's hook JSON on stdin and
always exits 0.

| Command | Agent event | Output |
| --- | --- | --- |
| `claude-code-stop` | Claude Code `Stop`, `SubagentStop` | `{"decision": "block", "reason": ...}` on FAIL, bad evidence after Playwright use, or a spec changed during the turn (never twice in a row); else `{}` or `{"systemMessage": ...}` |
| `codex-stop` | Codex `Stop` | as above |
| `gemini-after-agent` | Gemini CLI `AfterAgent` | `{"decision": "deny", "reason": ...}` on FAIL, else `{}` or `{"systemMessage": ...}` |
| `claude-code-record` | Claude Code `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied` | nothing; appends to the evidence log |
| `codex-record` | Codex `UserPromptSubmit`, `PostToolUse` | nothing; appends to the evidence log |
| `gemini-record` | Gemini CLI `BeforeAgent`, `AfterTool` | `{}`; appends to the evidence log |
| `claude-code-turn-start` | Claude Code `UserPromptSubmit` (synchronous) | nothing; saves the turn's task spec to the state directory |
| `codex-turn-start` | Codex `UserPromptSubmit` (synchronous) | nothing; as above |
| `gemini-turn-start` | Gemini CLI `BeforeAgent` | `{}`; as above |

| Option | Description |
| --- | --- |
| `--log-dir DIR` | Evidence-log directory (absolute). Record hooks default to `~/.evidence-judge/sessions`; stop hooks log the final answer only when this or `EVJ_HOOK_LOG_DIR` is set |
| `--state-dir DIR` | The hooks' private state (absolute): spec snapshots, the evidence-log key and head anchors. Default `EVJ_HOOK_STATE_DIR`, else `$CLAUDE_PLUGIN_DATA/state`, else `~/.evidence-judge/state`. Keep it out of the agent's reach |

Settings are `EVJ_HOOK_*` environment variables; see the
[Claude Code guide](guides/claude-code.md#configuration).
