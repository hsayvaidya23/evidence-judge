---
description: Run Evidence Judge as an MCP server so Claude Code, Codex CLI, Gemini CLI or any MCP client can judge, inspect and explain recorded browser-agent runs as tools.
---

# MCP server

`evidence-judge mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. A
coding agent that drives a browser (with Playwright MCP, say) can hand the recording to Evidence
Judge and get a verdict back as a tool result, instead of trusting its own account of the run.

!!! info "Not released yet"

    v0.1.0 is not released yet, so every command on this page installs from GitHub, not PyPI.
    To pin what your client runs, add a commit SHA to the URL:
    `git+https://github.com/hsayvaidya23/evidence-judge@<sha>`.

```bash
pip install "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge"
evidence-judge mcp                  # stdio, files under the working directory
evidence-judge mcp --root ./runs --root ./specs
evidence-judge mcp --http --port 8000   # streamable HTTP on http://127.0.0.1:8000/mcp
```

The server is built on the official MCP Python SDK (2.x). It speaks stdio by default; `--http`
serves streamable HTTP on `127.0.0.1` only, with DNS-rebinding protection. See the
[CLI reference](../cli.md#evidence-judge-mcp) for every option.

## Connect a client

Each snippet runs the package from GitHub with `uvx`. To run a checkout instead, replace the
command with `uv run --directory /path/to/evidence-judge --extra mcp evidence-judge mcp --root
"$PWD"`; `--directory` changes the working directory, so name the root explicitly.

=== "Claude Code"

    ```bash
    claude mcp add evidence-judge -- uvx --python 3.12 --from "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge" evidence-judge mcp
    ```

    `--scope project` writes the entry to `.mcp.json` at the project root so the whole team
    gets it; `--scope user` makes it available in every project. The `.mcp.json` form:

    ```json title=".mcp.json"
    {
      "mcpServers": {
        "evidence-judge": {
          "command": "uvx",
          "args": ["--python", "3.12", "--from", "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge", "evidence-judge", "mcp"]
        }
      }
    }
    ```

    For an HTTP server you started yourself:
    `claude mcp add --transport http evidence-judge http://127.0.0.1:8000/mcp`.
    Reference: [Claude Code MCP docs](https://code.claude.com/docs/en/mcp.md).

=== "Codex CLI"

    ```toml title="~/.codex/config.toml"
    [mcp_servers.evidence-judge]
    command = "uvx"
    args = ["--python", "3.12", "--from", "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge", "evidence-judge", "mcp"]
    startup_timeout_sec = 30   # the first uvx run clones and builds the package
    tool_timeout_sec = 120     # judging a large trace can take a while
    ```

    Or `codex mcp add evidence-judge -- uvx --python 3.12 --from "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge" evidence-judge mcp`.
    A trusted project's `.codex/config.toml` takes the same table. Over HTTP:
    `url = "http://127.0.0.1:8000/mcp"` instead of `command` and `args`.
    Reference: [Codex MCP docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

=== "Gemini CLI"

    ```json title="~/.gemini/settings.json"
    {
      "mcpServers": {
        "evidence-judge": {
          "command": "uvx",
          "args": ["--python", "3.12", "--from", "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge", "evidence-judge", "mcp"],
          "timeout": 120000
        }
      }
    }
    ```

    Or add it from the command line (the default scope is `project`, which writes
    `.gemini/settings.json`; `--` keeps `--from` away from Gemini's own options):

    ```bash
    gemini mcp add -s user evidence-judge uvx -- --python 3.12 --from "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge" evidence-judge mcp
    ```

    Over HTTP: `"httpUrl": "http://127.0.0.1:8000/mcp"`. `includeTools` and `excludeTools`
    narrow the tool list. Reference: [Gemini CLI MCP docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md).

=== "Other clients"

    Any MCP client that launches stdio servers works with the command
    `evidence-judge mcp` (or the `uvx` form above). Clients that connect over HTTP use
    `http://127.0.0.1:<port>/mcp` after `evidence-judge mcp --http --port <port>`.

Each client launches the server in its own working directory (Claude Code and Gemini CLI use
the project, Codex its `cwd` setting or the directory it started in), which becomes the
default root. Add `--root` arguments to allow other directories.

## Tools

Every tool returns structured content matching its output schema, plus a short text summary
for the model to read. A problem the caller can fix (a missing file, a path outside the roots,
an invalid spec, unreadable evidence) comes back as a tool error (`isError: true`) with a
message that says what to change; it never ends the session.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `judge_run` | `trace`, `har`, `browser_use` (at least one); `answer` or `answer_file`; `reasoning_file`; `task`; `spec` or `spec_yaml`; `site`; `offline` (default `true`); `require_tamper_evidence`; `allow_mismatch`; `run_id`; `json_report`; `html_report`; `overwrite` (default `false`) | `verdict` (the [verdict JSON](../reference/verdict-schema.md): outcome, score, summary, `tamper_detection`, each claim and expectation with its citations), `reports` written, `redaction`, `used_llm` |
| `inspect_run` | `trace`, `har`, `browser_use` | `sources` with their metadata, `captured` channels, `counts` (actions, requests, mutations, snapshots, screenshots, console), state-changing requests, `injections` (calls that made evidence the agent's own, with script sha256), load `warnings` |
| `explain_verdict` | `verdict_path` (a verdict JSON) | Each contradicted or unproven claim, unmet or unknown spec entry, dimension finding and tamper-detection limit with the evidence it cites, most decisive first, plus the same as Markdown in `text` |
| `validate_spec` | `path` or `spec_yaml` | `valid`, the spec's id and entry counts, and `errors` with dotted `location`, `line` and `column`. An invalid spec is a result, not a tool error |
| `find_recordings` | `directory` (default: the first root), `limit` (1 to 200, default 20) | Playwright trace zips, HAR files and browser-use histories, newest first, plus unzipped trace directories |

`tools/list` carries the full input and output JSON Schemas, with a description for every
parameter. `judge_run` is annotated `destructiveHint: true` because it can replace an earlier
report; every other tool is read-only.

Text the tools quote from the recorded pages or from the agent (the verdict summary, claim
quotes, reasons, evidence excerpts and warnings) comes wrapped in `<untrusted_data>` tags in
the text summary. A tag lookalike inside the text is defanged and cannot close the fence. The
server's instructions and the tool descriptions tell the calling model that this text is data,
never instructions. The structured content carries the same fields unwrapped.

## Judging a Playwright MCP run

[Playwright MCP](https://github.com/microsoft/playwright-mcp) records a trace when started with
the `devtools` capability: the agent calls `browser_start_tracing` before the task and
`browser_stop_tracing` after it, and the trace lands in `<output-dir>/traces/` as loose files
(`trace-<id>.trace`, `trace-<id>.network`, `resources/`).

```bash
npx @playwright/mcp@latest --caps devtools --output-dir .playwright-mcp
```

`find_recordings` lists such a directory as `playwright_trace_dir`. Zip one trace to judge it:

```bash
cd .playwright-mcp/traces
zip -r ../run.zip trace-<id>.trace trace-<id>.network resources
```

Then ask the agent to judge it, for example: *"Call judge_run with trace .playwright-mcp/run.zip,
the answer you gave, and spec specs/checkout.yaml, then explain any failure."* With no spec,
pass `site` so a run can pass; see [Task specs](../task-specs.md).

## Security

- **Roots.** The server reads and writes only inside its roots (`--root`, default the working
  directory). Every path is resolved with all symlinks followed before the check, so a symlink
  or `..` cannot lead out. Reports are never written through a symlink, and their directory
  must already exist. Relative paths resolve against the first root.
- **No clobbering.** `json_report` and `html_report` replace an existing file only when it is an
  earlier evidence-judge report: a verdict JSON that validates, or an HTML page with the
  report's generator tag. Any other file is refused unless the call passes `overwrite: true`.
- **Untrusted text is fenced.** Page- and agent-derived text in results is wrapped in
  `<untrusted_data>`, as described under [Tools](#tools).
- **No agent content runs.** Recordings are parsed as data with the same size limits as the CLI
  (trace entry, total and compression-ratio caps, 200 MB HAR, 50 MB browser-use history);
  specs over 1 MB and YAML anchors or aliases are refused, and verdict files over 50 MB are not
  read.
- **Offline by default.** `judge_run` calls a model only when the client passes
  `offline: false` and the server has one configured (`ANTHROPIC_API_KEY` in the server's
  environment). `--offline-only` refuses such calls outright.
- **Redaction** follows `EVJ_REDACT` (on by default): evidence, answers and specs are redacted
  before judging, so the verdict a client sees holds HMAC fingerprints, not secrets. A tool
  argument cannot turn it off. The HTML report embeds screenshots unredacted, as in the CLI.
- **HTTP** binds to `127.0.0.1` only and has no authentication; any local process can call it.
  Prefer stdio, which only the launching client can reach.

## Adding tools

`evidence_judge.mcp.build_server(config, toolsets)` takes `Toolset` callables, each of which
registers tools on the server and shares the same `ServerConfig` (roots, settings, model
policy):

```python
from evidence_judge.mcp import DEFAULT_TOOLSETS, Roots, ServerConfig, build_server


def my_tools(server, config: ServerConfig) -> None:
    @server.tool()
    def roots() -> list[str]:
        return [str(d) for d in config.roots.dirs]


server = build_server(ServerConfig(roots=Roots.of(["."])), (*DEFAULT_TOOLSETS, my_tools))
server.run("stdio")
```
