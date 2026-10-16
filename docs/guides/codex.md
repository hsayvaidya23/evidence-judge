---
description: Use Evidence Judge with the OpenAI Codex CLI - a Stop hook that checks the answer against the recorded browser trace and, with a session spec, an independent re-run of the session's tests judged with its evidence log and rollout, an evidence-log recorder and the MCP server.
---

# Codex CLI

Codex CLI (0.162) has a hooks system that is on by default and uses the same shape as Claude
Code's. Its `Stop` hook can keep Codex working: `{"decision": "block", "reason": ...}` starts
a continuation turn with `reason` as the prompt. Evidence Judge ships two Codex hooks:

| Command | Codex event | Does |
| --- | --- | --- |
| `evidence-judge hook codex-stop` | `Stop` | Judges the newest trace recorded during the turn; blocks on FAIL |
| `evidence-judge hook codex-record` | `PostToolUse`, `UserPromptSubmit` | Appends the call or prompt to the session's evidence log |
| `evidence-judge hook codex-guard` | `PreToolUse` | Denies writes to agent configuration, the `evidence-judge` install and evidence logs |

Codex's older `notify` program is not needed: it fires only on `agent-turn-complete`, is
fire-and-forget and cannot block.

## Install

```bash
uv tool install --python 3.12 "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge"
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
            "command": "evidence-judge hook codex-turn-start --state-dir \"$HOME/.evidence-judge/state\"",
            "timeout": 30
          },
          {
            "type": "command",
            "command": "evidence-judge hook codex-record --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
            "async": true
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "evidence-judge hook codex-guard",
            "timeout": 15
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "evidence-judge hook codex-record --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
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
            "command": "evidence-judge hook codex-stop --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
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

`codex-turn-start` must not be `async`: it saves the task spec before the agent acts, so the
stop hook judges with the spec the turn started with even if the agent edits it.

Drop `--log-dir` from the `codex-stop` command if you do not want the final answer written to
the evidence log; the stop decision does not depend on it.

`codex-guard` denies shell commands and `apply_patch` calls that write agent configuration
(`~/.codex/`, `.codex/`, `.claude/settings*.json`, `.mcp.json`, hook files), the
`evidence-judge` install or the evidence logs: an agent running as your OS user could
otherwise delete these hooks from `~/.codex/hooks.json` and finish unchecked. Codex lets a
`PreToolUse` hook that fails or times out through, so the guard decides from the call alone
and allows anything it cannot read; the session judge still reports such a write as harness
tampering. Hooks an administrator puts in the managed `requirements.toml` are the stronger
control, because the agent cannot edit them
([`pre_tool_use.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/pre_tool_use.rs)).

## What `codex-stop` does

It is the same check as the [Claude Code Stop hook](claude-code.md#what-the-stop-hook-does):

- `stop_hook_active` true: the answer is still judged, but never blocked twice in a row. If it
  still fails, the stop goes through with a warning to the user that says what is wrong.
- The turn's start comes from the rollout file at `transcript_path` (the `task_started`
  record of the hook's `turn_id`). The rollout format is internal to Codex and changes between
  releases; when it cannot be read, traces from the last 30 minutes count.
- If a `browser_stop_tracing` output in the rollout reported trace files this turn, only those
  are judged. Otherwise every trace modified this turn under `.playwright-mcp/`,
  `.evidence-judge/traces/`, `test-results/` or `EVJ_HOOK_TRACE_DIRS` is judged, offline,
  against `last_assistant_message`. The judge uses the spec saved by `codex-turn-start`, or the
  live spec when that hook is not installed.
- FAIL, bad evidence after a Playwright tool call (no trace, or a missing, junk or stale one),
  or a spec changed during the turn print `{"decision": "block", "reason": ...}`, and Codex
  continues with the reason as its next prompt. Anything else prints `{}` or a
  `systemMessage`, which Codex shows as a warning.
- Only the hook's own problems allow the stop with a notice: bad input, invalid settings, a
  timeout or a judge crash. The hook always exits 0 with JSON on stdout, which Codex requires
  for `Stop`.

The `EVJ_HOOK_*` settings are the same as for
[Claude Code](claude-code.md#configuration).

With a session spec whose `rerun:` block names the test command, `codex-stop` also runs the
suite itself, on a copy of the project in a clean environment, and judges the answer against
the runner's own report, reading the session from the evidence log and the rollout at
`transcript_path` ([Re-running the tests](claude-code.md#re-running-the-tests)). It runs at
every stop, so the `Stop` hook's `timeout` above and `EVJ_HOOK_TIMEOUT_S` must cover the suite.
`codex-turn-start` fingerprints `.venv`, `venv` and `node_modules` when the turn starts; a
change to them by the stop is NEEDS REVIEW. Gitignored files the suite needs (a generated
`_version.py`) go in `rerun.include`.

If `evidence-judge` is not on `PATH`, the hook command fails to start. Codex treats any exit
code other than 0 and 2 as a failed hook and carries on, so a missing CLI never blocks Codex.

## The evidence log

`codex-record` writes the shared log format version 1
([details](claude-code.md#the-evidence-log)) to
`~/.evidence-judge/sessions/<session_id>.jsonl`, with `"agent": "codex"`. Codex's
`PostToolUse` payload documents `tool_response` but not an exit-code field, so `exit_code` is
taken from `exit_code` or `exitCode` when the payload carries one. Otherwise it is read from the
`Process exited with code N` line of the header Codex writes before the command's own
`Output:`, so an echo in the output cannot supply or change it. With neither, it is `null`. With
`--state-dir`, lines are HMAC-chained and anchored as described for
[Claude Code](claude-code.md#the-evidence-log). For `apply_patch`, `result.changes` keeps the patch.

## What Codex can and cannot do

| | Codex CLI 0.162 |
| --- | --- |
| Block a stop and continue | Yes: `Stop` with `decision: "block"` |
| Record every tool call | Yes: `PostToolUse` (`async: true` keeps it off the critical path) |
| Exit code in the hook payload | Not documented; the payload field when present, else the `Process exited with code N` line |
| `notify` | Report only; cannot block |
| Plugin bundle | Not shipped here; use the hooks file above |

## What the hooks can and cannot guarantee

The [Claude Code guide](claude-code.md#what-the-hooks-can-and-cannot-guarantee) explains the
limits; they apply to Codex as written. The Codex-specific part is the sandbox. With
`--sandbox workspace-write` (or `sandbox_mode = "workspace-write"`), the agent's commands can
write only the workspace and temporary directories, so `~/.evidence-judge/state` and the
evidence logs are out of their write reach. Reads are not confined: the agent could still read
the log key, which is why `session judge` counts a log as `full` tamper evidence only with
`--trusted-log`. For that, run Codex in a container or as another OS user that cannot see
`~/.evidence-judge/`. `--dangerously-bypass-approvals-and-sandbox` removes these guarantees.

Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[advanced config (`notify`)](https://learn.chatgpt.com/docs/config-file/config-advanced),
[hook output schema](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/schema.rs).
