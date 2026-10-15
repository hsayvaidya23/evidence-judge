---
description: Use Evidence Judge with Google's Gemini CLI - an extension whose AfterAgent hook makes Gemini retry when the recorded browser trace contradicts its answer, plus an evidence-log recorder and the MCP server.
---

# Gemini CLI

Gemini CLI has hooks, and its `AfterAgent` hook can reject a final response:
`{"decision": "deny", "reason": ...}` makes Gemini retry with `reason` as a new prompt. That is
the equivalent of Claude Code's `Stop` hook, and Evidence Judge ships an adapter for it:

| Command | Gemini event | Does |
| --- | --- | --- |
| `evidence-judge hook gemini-after-agent` | `AfterAgent` | Judges the newest trace recorded during the turn; denies the response on FAIL |
| `evidence-judge hook gemini-record` | `AfterTool`, `BeforeAgent` | Appends the call or prompt to the session's evidence log |

The adapters are tested against JSON fixtures built from Gemini's documented hook schemas, not
against a live Gemini CLI.

## Install

```bash
uv tool install "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge"
git clone https://github.com/hsayvaidya23/evidence-judge
gemini extensions link evidence-judge/plugins/gemini-cli/evidence-judge
```

The extension (`plugins/gemini-cli/evidence-judge`) contains a `gemini-extension.json` that
registers the MCP server and a `hooks/hooks.json` with the three hooks below.

## Or configure it by hand

In `~/.gemini/settings.json` or a project's `.gemini/settings.json`:

```json
{
  "mcpServers": {
    "evidence-judge": { "command": "evidence-judge", "args": ["mcp"] }
  },
  "hooks": {
    "BeforeAgent": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "evidence-judge-turn-start",
            "type": "command",
            "command": "evidence-judge hook gemini-turn-start --state-dir \"$HOME/.evidence-judge/state\"",
            "timeout": 30000
          },
          {
            "name": "evidence-judge-record-prompt",
            "type": "command",
            "command": "evidence-judge hook gemini-record --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
            "timeout": 10000
          }
        ]
      }
    ],
    "AfterTool": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "evidence-judge-record",
            "type": "command",
            "command": "evidence-judge hook gemini-record --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
            "timeout": 10000
          }
        ]
      }
    ],
    "AfterAgent": [
      {
        "matcher": "*",
        "hooks": [
          {
            "name": "evidence-judge-check",
            "type": "command",
            "command": "evidence-judge hook gemini-after-agent --log-dir \"$HOME/.evidence-judge/sessions\" --state-dir \"$HOME/.evidence-judge/state\"",
            "timeout": 90000
          }
        ]
      }
    ]
  }
}
```

Gemini hook timeouts are in **milliseconds** (default 60,000). `gemini mcp add evidence-judge
evidence-judge mcp` registers the server from a shell instead.

## What `gemini-after-agent` does

The same check as the [Claude Code Stop hook](claude-code.md#what-the-stop-hook-does), with
Gemini's fields:

- `stop_hook_active` true (Gemini is already retrying): the response is still judged but never
  denied twice in a row. If it still fails, it goes through with a warning to the user that
  says what is wrong.
- `gemini-turn-start` on `BeforeAgent` saves the spec the turn starts with; the judge uses
  that copy, and a spec changed during the turn is denied.
- The answer is `prompt_response`; the task is `prompt`.
- Gemini's input has no turn start, so every trace modified in the last 30 minutes
  (`EVJ_HOOK_MAX_TRACE_AGE_S`) is judged, and a trace whose own events are older is rejected.
- Gemini's hook input does not show which tools ran, so the hook cannot tell that Playwright
  was used. No trace, or a junk one, is only a notice here, never a denial.
- FAIL: `{"decision": "deny", "reason": ...}`, and Gemini retries with the reason. PASS and
  NEEDS REVIEW: `{}` or a `systemMessage`. The hook's own problems (bad input, a timeout, a
  judge crash) allow the response with a notice; stdout always holds exactly one JSON object,
  as Gemini requires.

Hooks run with a sanitized environment plus `GEMINI_PROJECT_DIR`. If you set
`security.environmentVariableRedaction`, allow the `EVJ_HOOK_*` variables you use (and `HOME`,
which the log path above relies on).

## The evidence log

`gemini-record` writes the shared log format version 1
([details](claude-code.md#the-evidence-log)) to
`~/.evidence-judge/sessions/<session_id>.jsonl`, with `"agent": "gemini"`. Gemini's
`tool_response` has no structured exit code; for `run_shell_command` the recorder reads the
`Exit Code: N` line of the result text, and leaves `exit_code` null for other tools. With
`--state-dir`, lines are HMAC-chained and anchored as described for
[Claude Code](claude-code.md#the-evidence-log).

## What Gemini CLI can and cannot do

| | Gemini CLI |
| --- | --- |
| Reject the final answer and retry | Yes: `AfterAgent` with `decision: "deny"` |
| Record every tool call | Yes: `AfterTool` (synchronous, about 0.3 s per call) |
| Exit code | Parsed from `Exit Code: N` in `run_shell_command` output |
| Turn start | Not in the hook input; a 30 minute window is used |
| Playwright use | Not in the hook input; missing or junk traces are only a notice |

## What the hooks can and cannot guarantee

The limits in the [Claude Code guide](claude-code.md#what-the-hooks-can-and-cannot-guarantee)
apply as written. Gemini CLI's sandbox (`--sandbox`, or `tools.sandbox` in settings) runs
tools in a container or a macOS Seatbelt profile that confines writes to the project. Keep
`~/.evidence-judge/` out of it, and run Gemini as an OS user that cannot read that directory if
you want `session judge --trusted-log`.

Sources: [hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md),
[hooks overview](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/index.md),
[extensions reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/extensions/reference.md),
[MCP servers](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md).
