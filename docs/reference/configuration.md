---
description: Every Evidence Judge setting - EVJ_* environment variables and ANTHROPIC_API_KEY - with types, defaults and what they do.
---

# Configuration

Settings come from environment variables, never from code. A dotenv file is read only when you
name it: the global `--env-file PATH` option (`evidence-judge --env-file ci.env judge ...`) or
`EVJ_ENV_FILE=PATH`. Real environment variables win over the file. A `.env` in the working
directory is never read on its own, so a checked-out repository (say, a pull request in the
GitHub Action) cannot turn off redaction or lower the review threshold; `.env.example` in the
repository lists every variable. The one other command-line override is `--llm` on `judge` and
`bench`, which picks `EVJ_LLM_BACKEND` for that run. Every variable except
`ANTHROPIC_API_KEY` has the `EVJ_` prefix. An invalid value exits with code `3`.

| Variable | Type | Default | Purpose |
| --- | --- | --- | --- |
| `EVJ_LLM_BACKEND` | `anthropic`, `claude-code`, `codex`, `gemini` | `anthropic` | Where LLM calls go. `anthropic` calls the API with `ANTHROPIC_API_KEY`; the others run that CLI headless on your existing login, with its tools off. See [LLM backends](../guides/llm-backends.md). The `--llm` option of `judge` and `bench` overrides it |
| `ANTHROPIC_API_KEY` | secret | – | Enables LLM claim extraction (and screenshot checks, if allowed) on the `anthropic` backend. Without it, that backend judges offline, unless `--llm anthropic` asked for it, which exits `4` |
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
