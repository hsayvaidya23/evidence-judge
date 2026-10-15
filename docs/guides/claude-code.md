---
description: Use Evidence Judge inside Claude Code - a plugin whose Stop hook checks the agent's answer against the Playwright trace it recorded, an evidence-log recorder, an MCP server and a skill.
---

# Claude Code

The Evidence Judge plugin puts the judge inside Claude Code's own loop. When a turn drove a
browser through Playwright MCP and left a trace, the plugin's `Stop` hook judges that trace
against Claude's final message. If the evidence contradicts the answer (FAIL), Claude is not
allowed to stop: it gets the reasons as its next instruction and keeps working.

The plugin ships four things:

| Part | What it does |
| --- | --- |
| `Stop` and `SubagentStop` hooks | Judge the newest trace recorded during the turn; block the stop on FAIL |
| Recorder hooks (`UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`) | Append every prompt and tool call to a per-session evidence log, asynchronously |
| MCP server (`evidence-judge mcp`) | Tools `judge_run`, `inspect_run`, `explain_verdict`, `validate_spec`, `find_recordings` |
| Skill `evidence-judge` | Teaches Claude to record traces, write a spec and read a verdict |

## Install

1. Install the CLI. The hooks and the MCP server run `evidence-judge` from your `PATH`:

    ```bash
    uv tool install "evidence-judge[mcp] @ git+https://github.com/hsayvaidya23/evidence-judge"
    ```

2. In Claude Code, add the repository as a plugin marketplace and install the plugin:

    ```text
    /plugin marketplace add hsayvaidya23/evidence-judge
    /plugin install evidence-judge@evidence-judge
    ```

    From a shell, the same is `claude plugin marketplace add hsayvaidya23/evidence-judge` and
    `claude plugin install evidence-judge@evidence-judge --scope project` (or `user`).

3. Let Playwright MCP record traces. Tracing needs its devtools capability:

    ```bash
    claude mcp add playwright -- npx @playwright/mcp@latest --caps=devtools
    ```

    The agent then calls `browser_start_tracing` before it navigates and
    `browser_stop_tracing` before it answers; the skill tells it to. Playwright MCP has no
    `--save-trace` flag: the trace is left as loose files in `<output dir>/traces/` (by default
    `.playwright-mcp/traces/trace-<timestamp>.trace`, a `.network` file and a shared
    `resources/` directory). The hook zips one trace's files itself before judging.

4. Optionally, add a task spec at `.evidence-judge/spec.yaml` (or `evidence-judge.yaml`) in
   the project. Without one the judge can still FAIL a contradicted claim, but it cannot PASS
   a run that names no site. See [Task specs](../task-specs.md).

If `evidence-judge` is not on `PATH`, the stop hook allows every stop and shows one line,
*"the evidence-judge CLI is not on PATH, so this answer was not checked"*; the recorder does
nothing.

## What the Stop hook does

On every `Stop` and `SubagentStop`:

1. If `stop_hook_active` is true (Claude is already continuing because a stop hook blocked),
   allow the stop. The hook never blocks twice in a row.
2. Find the turn's start: the time of the last user prompt in the transcript. Without a
   readable transcript, only traces from the last 30 minutes count.
3. Look for the newest trace modified since then under `.playwright-mcp/`,
   `.evidence-judge/traces/`, `test-results/` and any `EVJ_HOOK_TRACE_DIRS`, without following
   symlinks. No trace: allow the stop, with a notice if a Playwright MCP tool was used this turn.
4. Judge it with `evidence-judge judge --offline` in a child process, with the agent's final
   message (`last_assistant_message`) as the answer, the last prompt as the task and the
   project spec if there is one.
5. FAIL: return `{"decision": "block", "reason": ...}`. The reason names the trace, the
   verdict's summary and up to five claims or spec expectations the evidence does not back up,
   with evidence ids. PASS and NEEDS REVIEW: allow the stop with a one-line `systemMessage`.

Claude Code caps consecutive stop-hook continuations (8 by default,
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`), on top of the hook's own `stop_hook_active` guard.

## Configuration

Set these in the environment Claude Code runs in, or in the `env` block of a
[settings file](https://code.claude.com/docs/en/settings):

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVJ_HOOK_ENABLED` | `true` | `false` turns every stop hook into a no-op |
| `EVJ_HOOK_TIMEOUT_S` | `60` | Wall-clock limit for one judgement; on timeout the stop is allowed |
| `EVJ_HOOK_BLOCK_ON_NEEDS_REVIEW` | `false` | Also keep the agent working on NEEDS REVIEW |
| `EVJ_HOOK_LLM` | `false` | Judge with the configured LLM (`ANTHROPIC_API_KEY`) instead of offline |
| `EVJ_HOOK_TRACE_DIRS` | `[]` | Extra trace directories, a JSON list, relative to the project root |
| `EVJ_HOOK_SPEC` | unset | Spec to use instead of the conventional locations |
| `EVJ_HOOK_MAX_TRACE_AGE_S` | `1800` | Newest trace age accepted when the turn's start is unknown |
| `EVJ_HOOK_LOG_DIR` | unset | Evidence-log directory when no `--log-dir` is given |

The plugin's hook has a 90 second timeout, so the judge's own limit always fires first.

## The evidence log

Claude Code's transcript keeps neither a shell command's exit code nor a record the agent
cannot edit. The recorder hooks write one JSON line per prompt, tool call and stop to
`${CLAUDE_PLUGIN_DATA}/evidence/<session_id>.jsonl` (the plugin's data directory under
`~/.claude/plugins/data/`, outside every workspace), in log format version 1:

```json
{"v":1,"agent":"claude-code","ts":"2026-10-15T09:00:00.000Z","session_id":"0b7e3c1a-…",
 "event":"tool","tool":"Bash","input":{"command":"npm test"},"exit_code":1,"status":"error",
 "stdout_sha256":"…","stdout_excerpt":"Exit code 1\nFAIL src/cart.test.ts…",
 "stderr_excerpt":"","output_truncated":false,"stdout_bytes":61,"cwd":"/work/shop",
 "tool_use_id":"toolu_…","duration_ms":2310,"seq":4,"prev":"9f2c…"}
```

- **Exit codes are never guessed.** `PostToolUse` of `Bash` without
  `returnCodeInterpretation` is `0` (Claude Code reports a non-zero exit as a failure); a
  `PostToolUseFailure` whose error starts `Exit code N` is `N`; anything else is `null`.
- **Excerpts are redacted and bounded** (head and tail, 2,000 characters).
  `stdout_sha256` hashes the full, raw output, and `output_truncated` is true whenever the
  excerpt was cut or redaction changed it, so a redacted excerpt never passes for the whole
  output.
- **Edits keep the patch, not the file**: `result` holds `structuredPatch`, `type` and
  `userModified`.
- **Hash chain.** `seq` counts lines from 0 and `prev` is the SHA-256 of the previous line, so
  an edit after the fact breaks the chain. Lines are written under a lock with fsync; the
  directory is `0700` and the file `0600`.

The recorder hooks are `async`, so they never slow the agent down.

## Security

- **Offline by default.** The judge runs with `--offline` and without `ANTHROPIC_API_KEY` in
  its environment unless `EVJ_HOOK_LLM=true`. Nothing leaves the machine.
- **Nothing the agent wrote is executed.** The answer goes to the judge in a temporary file;
  the task travels as a single `--task=` argument, never through a shell.
- **Bounded.** Hook input over 8 MB is refused; trace search does not follow symlinks and
  stops at a depth of 5 and 20,000 entries; a packed trace is limited to 512 MB and only
  includes files inside its own directory.
- **Fails open.** Bad input, a timeout, a judge crash or an unreadable trace allow the stop
  with a one-line notice. A judge problem never traps the agent.
- **Untrusted page text.** The block reason may quote checker reasons that repeat page text.
  It tells Claude to treat that text as data, and evidence excerpts themselves are never
  forwarded.

## Limits

- The hook reads the transcript only for the turn's start time and as a fallback for the final
  message. Claude Code documents the transcript format as internal, so if a release changes it,
  the hook falls back to the 30 minute window rather than failing.
- A turn that browsed without tracing is not judged; the hook says so.
- A `SubagentStop` and the parent's `Stop` can judge the same trace, each against its own
  final message.

Sources: [hooks reference](https://code.claude.com/docs/en/hooks),
[plugins reference](https://code.claude.com/docs/en/plugins/manifest-reference),
[marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference),
[skills](https://code.claude.com/docs/en/skills),
[Playwright MCP](https://github.com/microsoft/playwright-mcp).
