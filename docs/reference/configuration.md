---
description: Every Evidence Judge setting - EVJ_* environment variables and ANTHROPIC_API_KEY - with types, defaults and what they do.
---

# Configuration

Settings come from environment variables or a `.env` file in the working directory (see
`.env.example` in the repository), never from code. The one command-line override is `--llm`
on `judge` and `bench`, which picks `EVJ_LLM_BACKEND` for that run. Every variable except
`ANTHROPIC_API_KEY` has the `EVJ_` prefix. An invalid value exits with code `3`.

| Variable | Type | Default | Purpose |
| --- | --- | --- | --- |
| `EVJ_LLM_BACKEND` | `anthropic`, `claude-code`, `codex`, `gemini` | `anthropic` | Where LLM calls go. `anthropic` calls the API with `ANTHROPIC_API_KEY`; the others run that CLI headless on your existing login, with its tools off. See [LLM backends](../guides/llm-backends.md). The `--llm` option of `judge` and `bench` overrides it |
| `ANTHROPIC_API_KEY` | secret | – | Enables LLM claim extraction (and screenshot checks, if allowed) on the `anthropic` backend. Without it, that backend judges offline |
| `EVJ_MODEL_FAST` | string | `claude-haiku-5-5` | Model for claim extraction |
| `EVJ_MODEL_STRONG` | string | `claude-sonnet-5-5` | Model for screenshot checks and the benchmark's transcript baseline |
| `EVJ_CLI_MODEL_FAST` | string | – | Model for claim extraction on a CLI backend. Unset: `claude-code` uses `EVJ_MODEL_FAST`; `codex` and `gemini` use the CLI's default |
| `EVJ_CLI_MODEL_STRONG` | string | – | Model for screenshot checks on a CLI backend. Unset: `claude-code` uses `EVJ_MODEL_STRONG`; `codex` and `gemini` use the CLI's default |
| `EVJ_CLI_TIMEOUT_S` | float > 0 | `180.0` | Time limit of one CLI-backend call, including the CLI's start-up, in seconds. A CLI still running then is killed with everything it started |
| `EVJ_MAX_COST_USD` | float > 0 | `0.25` | Hard LLM budget per run |
| `EVJ_REQUEST_TIMEOUT_S` | float | `60.0` | Timeout of one LLM request, in seconds |
| `EVJ_MAX_RETRIES` | int ≥ 0 | `3` | Retries for 408/409/429/5xx and connection errors, with backoff and `retry-after` |
| `EVJ_MAX_CONCURRENT_LLM_CALLS` | int ≥ 1 | `4` | LLM requests in flight at once (cache hits are not limited) |
| `EVJ_CACHE_DIR` | path | per-user cache dir | LLM response cache **and the redaction key file** `redaction.key` |
| `EVJ_REDACT` | bool | `true` | Redact secrets and PII before anything else sees the run |
| `EVJ_REDACTION_KEY` | secret | key file, else per process | HMAC key so redacted values still match across evidence |
| `EVJ_SCREENSHOTS_TO_LLM` | bool | `false` | Send screenshots to the vision model. Off because pixels are never redacted; `judge` prints a notice when on |
| `EVJ_MAX_BODY_CHARS` | int | `20000` | Characters of each request/response body kept as evidence |
| `EVJ_REVIEW_COVERAGE_THRESHOLD` | float 0..1 | `0.6` | Below this share of the answer turned into checkable claims, the verdict is `NEEDS_REVIEW` |

## The cache directory

The default `EVJ_CACHE_DIR` is never relative to the working directory:

| Platform | Default |
| --- | --- |
| `XDG_CACHE_HOME` set (absolute) | `$XDG_CACHE_HOME/evidence-judge` |
| macOS | `~/Library/Caches/evidence-judge` |
| Windows | `%LOCALAPPDATA%\evidence-judge` |
| Linux and others | `~/.cache/evidence-judge` |

## The redaction key

The HMAC key that makes redaction fingerprints stable is chosen in this order:

1. `EVJ_REDACTION_KEY`, when set. Use it to share one key across machines and CI, so redacted
   prompts (and so LLM cache keys) match everywhere.
2. When a run uses the LLM cache, a random 32-byte key stored at
   `<EVJ_CACHE_DIR>/redaction.key`, created with mode 0600 on first use. A key file readable by
   other users logs a warning; a corrupt one exits with code `3` (delete it to regenerate).
3. Otherwise a random key per process, with a one-line warning if the cache directory is not
   writable. The one exception is an offline `bench`, which keys its tokens with a digest of the
   files in its run directories so its results are byte-reproducible (see
   [the benchmark](../benchmark.md#running-it)).

Offline runs and `inspect` never read or create the key file. Treat the key and the key file as
secrets.

## Agent hooks

The hooks that `evidence-judge hook ...` runs for Claude Code, Codex CLI and Gemini CLI read
`EVJ_HOOK_*` variables from the environment the agent starts them with, never from a `.env`
file. The judge they start inherits none of the other `EVJ_*` variables unless
`EVJ_HOOK_PASS_ENV` names them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVJ_HOOK_ENABLED` | `true` | `false` turns every stop hook into a no-op |
| `EVJ_HOOK_GUARD` | `true` | `false` lets the guard hook allow writes to agent configuration and the judge's install |
| `EVJ_HOOK_ALLOW_MISSING_JUDGE` | unset | `1` lets the Stop hook allow the stop with a notice when the pinned CLI is gone or changed |
| `EVJ_HOOK_TIMEOUT_S` | `60` | Wall-clock limit for judging the turn's traces; on timeout the stop is allowed with a notice |
| `EVJ_HOOK_BLOCK_ON_NEEDS_REVIEW` | `false` | Also keep the agent working on NEEDS REVIEW |
| `EVJ_HOOK_LLM` | `false` | Judge with the configured LLM (`ANTHROPIC_API_KEY`) instead of offline |
| `EVJ_HOOK_TRACE_DIRS` | `[]` | Extra trace directories, a JSON list, relative to the project root |
| `EVJ_HOOK_SPEC` | unset | Spec to use instead of the conventional locations. An absolute path outside the workspace keeps it out of the agent's reach (recommended) |
| `EVJ_HOOK_ON_BAD_EVIDENCE` | `block` | When Playwright was used but its traces are missing, unreadable, older than the turn or refused by the judge: `block` or `notice` |
| `EVJ_HOOK_BLOCK_ON_SPEC_CHANGE` | `true` | Keep the agent working when the spec changed during the turn; `false` only warns |
| `EVJ_HOOK_SESSION_SPEC` | unset | Session spec whose `rerun:` block the Stop hook runs, instead of `.evidence-judge/session.yaml`; see [Re-running the tests](../guides/claude-code.md#re-running-the-tests) |
| `EVJ_HOOK_PASS_ENV` | `[]` | `EVJ_*` variables the judge may inherit, a JSON list; every other `EVJ_*` is dropped |
| `EVJ_HOOK_MAX_TRACE_AGE_S` | `1800` | Newest trace age accepted when the turn's start is unknown |
| `EVJ_HOOK_LOG_DIR` | unset | Evidence-log directory when no `--log-dir` is given |
| `EVJ_HOOK_STATE_DIR` | `$CLAUDE_PLUGIN_DATA/state`, else `~/.evidence-judge/state` | The hooks' private state (spec snapshots, log key, log anchors) when no `--state-dir` is given |

`EVJ_HOOK_ALLOW_MISSING_JUDGE` is read by the plugins' `run-hook.sh`; set it where the agent
harness runs, not where the agent can. See the [Claude Code](../guides/claude-code.md),
[Codex CLI](../guides/codex.md) and [Gemini CLI](../guides/gemini-cli.md) guides.

## Integration variables

The integrations read their own standard variables:

| Integration | Variables |
| --- | --- |
| OpenTelemetry (`--otel`) | `OTEL_*` (e.g. `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_SDK_DISABLED`) and `TRACEPARENT` |
| Langfuse (`--langfuse-trace-id`) | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` |
| Arize Phoenix | `PHOENIX_*`, read by the Phoenix client |

See [Integrations](../integrations.md).

## Example `.env`

```bash title=".env"
ANTHROPIC_API_KEY=sk-ant-...
EVJ_MAX_COST_USD=0.10
EVJ_MAX_CONCURRENT_LLM_CALLS=2
# EVJ_SCREENSHOTS_TO_LLM=false
# EVJ_REDACTION_KEY=...
```

Never commit `.env`.
