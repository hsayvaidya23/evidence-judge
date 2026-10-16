---
description: Install the evidence-judge CLI with uv, pip or Docker, and pick the optional extras you need.
---

# Install

Evidence Judge needs **Python 3.12 or newer** (it is tested on 3.12 and 3.13).

!!! info "Not released yet"

    v0.1.0 is not released yet, so nothing is on PyPI: install from the GitHub repository, as
    below. Do not install `evidence-judge` from PyPI until the release notes say it is there.
    To pin a commit, add it to the URL: `git+https://github.com/hsayvaidya23/evidence-judge@<sha>`.

=== "uv (recommended)"

    ```bash
    uv tool install --python 3.12 "evidence-judge[anthropic] @ git+https://github.com/hsayvaidya23/evidence-judge"
    ```

    `uv tool install` uses uv's default interpreter, not a `.python-version` file, so
    `--python 3.12` keeps it from picking an older Python (any 3.12 or newer works).

=== "pip"

    ```bash
    pip install "evidence-judge[anthropic] @ git+https://github.com/hsayvaidya23/evidence-judge"
    ```

=== "From a checkout"

    ```bash
    git clone https://github.com/hsayvaidya23/evidence-judge
    cd evidence-judge
    uv sync                         # dev environment, all extras
    uv run evidence-judge --help
    ```

=== "Docker"

    ```bash
    git clone https://github.com/hsayvaidya23/evidence-judge
    cd evidence-judge
    docker build -t evidence-judge .
    docker run --rm evidence-judge version
    ```

    See the [Docker guide](../guides/docker.md) for mounting recordings and passing settings.

Check it works:

```console
$ evidence-judge version
evidence-judge 0.1.0.dev0
```

## Extras

The core install is small: pydantic, typer, rich, jinja2 and PyYAML. Everything else is an
optional extra.

| Extra | Adds | Needed for |
| --- | --- | --- |
| `anthropic` | Anthropic SDK | LLM claim extraction and (opt-in) screenshot checks |
| `otel` | OpenTelemetry API + SDK | `--otel` and `emit_verdict(...)` |
| `langfuse` | Langfuse SDK | `--langfuse-trace-id` and `push_to_langfuse(...)` |
| `phoenix` | `arize-phoenix-client` | `push_to_phoenix(...)` |
| `inspect` | Inspect AI | `evidence_judge_scorer()` |
| `mcp` | MCP Python SDK | `evidence-judge mcp` ([MCP server](../guides/mcp.md)) |
| `all` | all of the above | everything |

Without the `anthropic` extra, or without `ANTHROPIC_API_KEY`, Evidence Judge judges offline:
heuristic claim extraction and deterministic checks only. See
[LLM vs offline](../concepts/llm-vs-offline.md).

## Optional: an API key

```bash
export ANTHROPIC_API_KEY=...   # or put it in a .env file next to where you run the CLI
```

That is the only credential the judge itself reads. The OpenTelemetry, Langfuse and Phoenix
integrations read their own (`OTEL_*`, `LANGFUSE_*`, `PHOENIX_*`; see
[Integrations](../integrations.md)), and only when you use them. Every other setting is an `EVJ_*` environment variable with a safe default; see
[Configuration](../reference/configuration.md).

Next: [record a run](record-a-run.md).
