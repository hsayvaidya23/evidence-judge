---
description: Use the Claude Code, OpenAI Codex or Gemini CLI you are already logged into as Evidence Judge's LLM, run headless with every tool off - setup, security model, exact flags and trade-offs.
---

# LLM backends

Evidence Judge needs an LLM for claim extraction and, if you allow it, screenshot checks (see
[LLM vs offline](../concepts/llm-vs-offline.md)). By default it calls the Anthropic API with
`ANTHROPIC_API_KEY`. If you have no API key but you are logged into a coding-agent CLI, it can
use that instead:

| Backend | Uses | Screenshots | Cost reported | Select with |
| --- | --- | --- | --- | --- |
| `anthropic` (default) | The Anthropic API and `ANTHROPIC_API_KEY` | Yes | Yes (token prices) | `EVJ_LLM_BACKEND=anthropic` |
| `claude-code` | The `claude` CLI and your Claude login | Yes | Yes (`total_cost_usd`) | `--llm claude-code` |
| `codex` | The `codex` CLI and your ChatGPT or OpenAI login | Yes | No (tokens only) | `--llm codex` |
| `gemini` | The `gemini` CLI and your Google login | No | No (tokens only) | `--llm gemini` |

Pick one explicitly: Evidence Judge never guesses a backend from what is installed.

```bash
evidence-judge judge --trace trace.zip --answer-file answer.txt --llm claude-code
EVJ_LLM_BACKEND=codex evidence-judge judge --trace trace.zip --answer-file answer.txt
evidence-judge session judge --claude-code latest --llm claude-code
```

In Python, set the backend on `Settings` and build the client as usual:

```python
from evidence_judge import Judge, Settings, build_llm

settings = Settings(llm_backend="codex")
judge = Judge(settings, build_llm(settings))
```

The rest of the pipeline does not change: redaction runs before any prompt is built, agent text is
fenced off as untrusted data, replies are validated against a schema (with one repair retry), and
a code check, not the model, decides every claim the evidence can decide.

## Setup

=== "Claude Code"

    1. Install Claude Code (2.1.248 or later, for `--restricted`) and log in once:
       `claude auth login`. Check with `claude auth status`.
    2. Judge with `--llm claude-code`. Models default to `EVJ_MODEL_FAST` /
       `EVJ_MODEL_STRONG`; set `EVJ_CLI_MODEL_FAST` / `EVJ_CLI_MODEL_STRONG` to use another
       name or an alias (`haiku`, `sonnet`, `opus`).

    If `ANTHROPIC_API_KEY` is set in the environment, Claude Code bills that key instead of your
    subscription; use the `anthropic` backend for API-key use, it is faster.

=== "Codex"

    1. Install the Codex CLI and log in: `codex login` (check with `codex login status`).
    2. Judge with `--llm codex`. Without `EVJ_CLI_MODEL_FAST` / `EVJ_CLI_MODEL_STRONG`, Codex
       uses its own default model.

    Your `~/.codex/config.toml` is not read for these calls (see below), so set the model with
    the `EVJ_CLI_MODEL_*` variables, not in the Codex config.

=== "Gemini"

    1. Install the Gemini CLI and sign in once by running `gemini` interactively (or set
       `GEMINI_API_KEY`).
    2. Judge with `--llm gemini`. Without `EVJ_CLI_MODEL_*`, Gemini picks its default model.

    The Gemini backend cannot send screenshots (see [Screenshots](#screenshots)).

A CLI that is not on `PATH` fails when the client is built; one that is not logged in fails on
the first call. Both exit with code `4` and say how to fix it, instead of judging offline.

## Security model

These CLIs are agents: left to their defaults they can run shell commands, read and write files,
fetch URLs and call MCP servers. The prompts Evidence Judge sends contain text written by the
agent being graded, which may try prompt injection. So every call takes the agent's capabilities
away and treats the CLI as a plain model:

- **No tools.** Each CLI runs with its tools off (exact flags below). With no tool, an injected
  instruction has nothing to act with: it can only change the reply, and the reply is validated
  against a schema and then checked against the recorded evidence by code.
- **An empty, throwaway working directory.** Each call runs in a fresh temporary directory that
  holds only what the call needs (the schema file, the screenshots for Codex, the settings and
  policy files for Gemini) and is deleted afterwards. Nothing from your project is in reach.
- **No user configuration that could act.** MCP servers, hooks, plugins, skills and project
  instruction files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) are switched off where the CLI
  allows it, so your own setup neither fires on these prompts nor sees them.
- **No shell, prompt on stdin.** The CLI is started with an argument vector, never through a
  shell. The prompt, which holds the agent text, is written to the CLI's stdin, so it never
  appears in the process list or in shell history. Only the fixed system prompt and the schema
  go on the command line.
- **Nothing persisted** where the CLI supports it (`--no-session-persistence`, `--ephemeral`).
- **Bounded.** `EVJ_CLI_TIMEOUT_S` (default 180 s) caps each call; a CLI still running then is
  killed together with every process it started. At most `EVJ_MAX_CONCURRENT_LLM_CALLS` CLIs
  run at once.
- **Redacted input.** `judge` and `bench` redact secrets and PII from the evidence and the agent's
  text before a backend is involved, exactly as for the API backend. Screenshots are never
  redacted, which is why they are sent only with `EVJ_SCREENSHOTS_TO_LLM=true`.

What this cannot change: the prompt still goes to the vendor behind the CLI, under that
account's terms, and the CLI vendor's own system prompt and policies still apply.

## Exact flags

Verified against Claude Code 2.1.300 and codex-cli 0.162.1. The Gemini flags follow the Gemini
CLI 0.64 reference; Gemini is tested against a fake CLI only.

=== "Claude Code"

    ```text
    claude -p --input-format stream-json --output-format stream-json --verbose
      --tools "" --restricted --safe-mode
      --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources ""
      --disable-slash-commands --permission-mode dontAsk --permission-prompts none
      --max-turns 2 --no-session-persistence
      --system-prompt <system prompt> --json-schema <schema>
      --max-budget-usd <budget headroom> [--model M] [--effort low|medium|high]
    ```

    | Flag | Why |
    | --- | --- |
    | `--input-format stream-json` | One user message as JSON on stdin, so screenshots go as base64 image blocks, never as files |
    | `--output-format stream-json --verbose` | Required with stream-json input; the last `result` event holds `structured_output`, `usage` and `total_cost_usd` |
    | `--tools ""` | No built-in tool. The CLI keeps only its internal `StructuredOutput` tool, which `--json-schema` needs |
    | `--restricted` | Ignores user, project and local settings and refuses code-running tools even if one were named |
    | `--safe-mode` | No `CLAUDE.md`, skills, plugins, hooks or custom agents |
    | `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` | No MCP server |
    | `--setting-sources ""` | No settings file, so no permission rules or hooks from one |
    | `--permission-mode dontAsk --permission-prompts none` | Anything that would ask for permission is denied |
    | `--max-turns 2` | The answer, plus one schema correction inside the CLI |
    | `--json-schema` | The reply must validate against the pydantic schema (closed objects) |
    | `--max-budget-usd` | The CLI stops itself at the headroom left in the run's budget |

    `--disallowedTools "*"` is deliberately not used: it also denies `StructuredOutput`, and the
    model then cannot answer in the schema. `--bare` is not used either: it ignores the
    subscription login and needs an API key.

=== "Codex"

    ```text
    codex exec --json
      --sandbox read-only --skip-git-repo-check --ephemeral
      --ignore-user-config --ignore-rules --color never
      -c approval_policy="never" -c web_search="disabled"
      -c features.<name>=false   (for each feature below)
      --cd <temp dir> -c developer_instructions=<system prompt>
      --output-schema <temp dir>/output-schema.json
      [--model M] [-c model_reasoning_effort="low"] [--image <temp dir>/screenshot-N.png ...]
      -
    ```

    | Flag | Why |
    | --- | --- |
    | `--sandbox read-only`, `-c approval_policy="never"` | Even if a tool slipped through, it could not write, and nothing can ask to escalate |
    | `-c features.<name>=false` | Off: `shell_tool`, `unified_exec`, `apps`, `plugins`, `remote_plugin`, `multi_agent`, `browser_use`, `browser_use_external`, `in_app_browser`, `computer_use`, `image_generation`, `view_image`, `hooks`, `memories`, `goals`, `tool_suggest`, `skill_search`, `sleep_tool`. A `-c` override that names a feature an older or newer Codex does not know is ignored, so the list does not break on upgrades |
    | `-c web_search="disabled"` | No web search tool |
    | `--ignore-user-config`, `--ignore-rules` | Your `config.toml` (MCP servers, hooks, notify, model) and exec-policy rules are not loaded; the login still is |
    | `--ephemeral` | No session file is written |
    | `-c developer_instructions=...` | The system prompt goes in as a developer-role message |
    | `--output-schema` | Codex sends the schema as a strict JSON schema: every object closed, every property required (optional fields are nullable) |
    | `--json` | JSONL events: the last `agent_message` item is the reply, `turn.completed` carries token usage, `turn.failed` an error |
    | `-` | The prompt is read from stdin |

    Codex still loads skills from `~/.agents/skills` into its context. With no tool to act
    with, they only cost input tokens.

=== "Gemini"

    ```text
    gemini --output-format json --approval-mode default
      --policy <temp dir>/deny-all-tools.toml
      --allowed-mcp-server-names evidence-judge-no-mcp
      --skip-trust [--model M] --prompt <closing instruction>
    ```

    with `GEMINI_SYSTEM_MD=<temp dir>/system.md` and `<temp dir>/.gemini/settings.json`:

    | Setting | Why |
    | --- | --- |
    | `deny-all-tools.toml`: `toolName = "*"`, `decision = "deny"` | The policy engine denies every tool, built-in and MCP |
    | `.gemini/settings.json`: `tools.core: []` | An empty built-in tool allow-list |
    | `hooksConfig.enabled: false`, `skills.enabled: false` | Your hooks and skills stay off |
    | `--allowed-mcp-server-names evidence-judge-no-mcp` | A name no server has, so no MCP server starts |
    | `--skip-trust` | Headless Gemini refuses untrusted folders; it also makes the workspace settings above apply |
    | `GEMINI_SYSTEM_MD` | Replaces Gemini's own system prompt with Evidence Judge's, plus the JSON schema the reply must follow (Gemini CLI has no schema flag) |
    | `--output-format json` | One object: `response`, `stats` (tokens per model) and, on failure, `error` |

    Exit codes `41` (authentication), `42` (input), `44` (sandbox) and `52` (configuration)
    become configuration errors; any other failure is an ordinary LLM error.

## Screenshots

With `EVJ_SCREENSHOTS_TO_LLM=true`, `claude-code` sends screenshots as base64 image blocks over
stdin and `codex` as `--image` files in the call's temporary directory. Gemini CLI reads images
only through its file tools, which stay off, so with `gemini` every screenshot check reports
*no evidence* (`the gemini backend cannot send images to the model, so screenshot checks are
unavailable with it`). It never passes a claim it could not look at.

## Trade-offs

| | `anthropic` | CLI backends |
| --- | --- | --- |
| Setup | An API key | A CLI you already use and are logged into |
| Latency per call | About 1 to 3 s | CLI start-up on top: about 3 s for Claude Code and 10 to 20 s for Codex in our runs |
| Budget (`EVJ_MAX_COST_USD`) | Enforced from token prices | `claude-code`: enforced from the cost the CLI reports (list prices; a subscription is not billed per call). `codex`, `gemini`: no cost is reported, so the dollar cap does not limit them; your plan's own limits apply |
| Output limit | `max_output_tokens` and `effort` per request | `effort` maps to `--effort` (Claude Code) and `model_reasoning_effort` (Codex); no CLI takes an output-token cap |
| Structured output | Constrained decoding against the schema | Claude Code and Codex validate against the schema natively; Gemini is instructed and validated, with one repair retry |
| Determinism | No temperature control in either case | Same; the CLI's own system prompt and updates can also change replies. The LLM cache (`EVJ_CACHE_DIR`) keys entries by backend and model, so a repeated run answers from disk |
| Retries | The SDK retries 408/409/429/5xx | Each CLI retries its own API errors; Evidence Judge does not retry again |

Use a CLI backend for local work and trying Evidence Judge out. For CI and benchmarks, prefer the
`anthropic` backend: it is faster, its budget always applies, and it does not depend on a
logged-in desktop session.

### Why the Claude Code CLI and not the Agent SDK

The Python `claude-agent-sdk` drives the same `claude` process over the same stream-json
protocol, so it would not add isolation or capabilities here. Calling the CLI directly avoids a
new dependency (and a second, bundled copy of Claude Code that can drift from the one you are
logged into), keeps all three backends on one code path, and leaves every flag visible above.

### Gemini with an API key

With a Gemini API key, calling the Gemini API directly with a response schema would be faster
and constrained like the `anthropic` backend. That backend does not exist yet; the `gemini`
backend is for a Google login without a key.

## Troubleshooting

| Message | Fix |
| --- | --- |
| `the codex backend needs the codex CLI on PATH` | Install the CLI, or put it on `PATH` for the process that runs Evidence Judge |
| `claude-code is not logged in or was refused` | Run `claude auth login` (or `codex login`, or `gemini` once) as the same user |
| `codex rejected the model` | Check `EVJ_CLI_MODEL_FAST` / `EVJ_CLI_MODEL_STRONG` against the models your plan offers |
| `did not answer within 180s (EVJ_CLI_TIMEOUT_S)` | Raise `EVJ_CLI_TIMEOUT_S`, or lower `EVJ_MAX_CONCURRENT_LLM_CALLS` if many CLIs start at once |
| `LLM budget would be exceeded` with `claude-code` | A model name with no listed price is reserved at the most expensive rate; use a full model name or an alias (`haiku`, `sonnet`, `opus`), or raise `EVJ_MAX_COST_USD` |
