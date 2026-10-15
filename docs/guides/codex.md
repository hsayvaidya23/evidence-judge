---
description: Use Evidence Judge with the OpenAI Codex CLI - a Stop hook that checks the answer against the recorded browser trace, an evidence-log recorder and the MCP server.
---

# Codex CLI

Codex CLI (0.162) has a hooks system that is on by default and uses the same shape as Claude
Code's. Its `Stop` hook can keep Codex working: `{"decision": "block", "reason": ...}` starts
a continuation turn with `reason` as the prompt. Evidence Judge ships two Codex hooks:

| Command | Codex event | Does |
| --- | --- | --- |
| `evidence-judge hook codex-stop` | `Stop` | Judges the newest trace recorded during the turn; blocks on FAIL |
| `evidence-judge hook codex-record` | `PostToolUse`, `UserPromptSubmit` | Appends the call or prompt to the session's evidence log |

Codex's older `notify` program is not needed: it fires only on `agent-turn-complete`, is
fire-and-forget and cannot block.

## Install

```bash
uv tool install "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge"
```

## Register the MCP server

```bash
codex mcp add evidence-judge -- evidence-judge mcp
```

or in `~/.codex/config.toml` (or a trusted project's `.codex/config.toml`):

```toml
[mcp_servers.evidence-judge]
command = "evidence-judge"
args = ["mcp"]
```

The server's tools are `judge_run`, `inspect_run`, `explain_verdict`, `validate_spec` and
`find_recordings`.

## Add the hooks

Put this in `~/.codex/hooks.json`, or in `<repo>/.codex/hooks.json` for one project (project
hooks load only when the project is trusted):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "evidence-judge hook codex-record --log-dir \"$HOME/.evidence-judge/sessions\"",
            "async": true
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "evidence-judge hook codex-record --log-dir \"$HOME/.evidence-judge/sessions\"",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "evidence-judge hook codex-stop --log-dir \"$HOME/.evidence-judge/sessions\"",
            "timeout": 90
          }
        ]
      }
    ]
  }
}
```

The same entries can live inline in `config.toml` as `[[hooks.Stop]]` tables with a nested
`[[hooks.Stop.hooks]]`. `timeout` is in seconds.

Codex runs only **trusted** hooks: approve them once in the TUI with `/hooks` (trust is pinned
to the hook's hash, so an edited hook needs approving again). Headless `codex exec` runs need
persisted trust or `--dangerously-bypass-hook-trust`.

Drop `--log-dir` from the `codex-stop` command if you do not want the final answer written to
the evidence log; the stop decision does not depend on it.

## What `codex-stop` does

It is the same check as the [Claude Code Stop hook](claude-code.md#what-the-stop-hook-does):

- `stop_hook_active` true: allow the stop, so the hook never blocks twice in a row.
- The turn's start comes from the rollout file at `transcript_path` (the `task_started`
  record of the hook's `turn_id`). The rollout format is internal to Codex and changes between
  releases; when it cannot be read, traces from the last 30 minutes count.
- The newest fresh trace under `.playwright-mcp/`, `.evidence-judge/traces/`, `test-results/`
  or `EVJ_HOOK_TRACE_DIRS` is judged offline against `last_assistant_message`, with the
  project's `.evidence-judge/spec.yaml` when there is one.
- FAIL prints `{"decision": "block", "reason": ...}` and Codex continues with the reason as its
  next prompt. Anything else prints `{}` or a `systemMessage`, which Codex shows as a warning.
- Every failure (bad input, timeout, judge crash) allows the stop. The hook always exits 0 with
  JSON on stdout, which Codex requires for `Stop`.

The `EVJ_HOOK_*` settings are the same as for
[Claude Code](claude-code.md#configuration).

If `evidence-judge` is not on `PATH`, the hook command fails to start. Codex treats any exit
code other than 0 and 2 as a failed hook and carries on, so a missing CLI never blocks Codex.

## The evidence log

`codex-record` writes the shared log format version 1
([details](claude-code.md#the-evidence-log)) to
`~/.evidence-judge/sessions/<session_id>.jsonl`, with `"agent": "codex"`. Codex's
`PostToolUse` payload documents `tool_response` but not an exit-code field, so `exit_code` is
recorded only when the payload carries `exit_code` or `exitCode`, and is `null` otherwise. It is
never parsed out of the output text. For `apply_patch`, `result.changes` keeps the patch.

## What Codex can and cannot do

| | Codex CLI 0.162 |
| --- | --- |
| Block a stop and continue | Yes: `Stop` with `decision: "block"` |
| Record every tool call | Yes: `PostToolUse` (`async: true` keeps it off the critical path) |
| Exit code in the hook payload | Not documented; recorded only when present |
| `notify` | Report only; cannot block |
| Plugin bundle | Not shipped here; use the hooks file above |

Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[advanced config (`notify`)](https://learn.chatgpt.com/docs/config-file/config-advanced),
[hook output schema](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/schema.rs).
