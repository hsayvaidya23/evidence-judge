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
| Guard hook (`PreToolUse`) | Denies writes to agent settings, hook files, the `evidence-judge` install and evidence logs (see [Keeping the hooks on](#keeping-the-hooks-on)) |
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

On every `UserPromptSubmit`, `claude-code-turn-start` (synchronous) saves the task spec the
turn starts with, or the fact that there is none, to the hooks' private state directory
(`${CLAUDE_PLUGIN_DATA}/state/<session_id>/`). It runs before the agent acts.

On every `Stop` and `SubagentStop`:

1. Find the turn's start: the time of the last user prompt in the transcript. Without a
   readable transcript, only traces from the last 30 minutes count.
2. Choose the traces to judge:
    - If `browser_stop_tracing` reported trace files in the transcript this turn, only those
      count, and each must exist.
    - Otherwise, every trace modified since the turn started counts, up to 10, under
      `.playwright-mcp/`, `.evidence-judge/traces/`, `test-results/` and any
      `EVJ_HOOK_TRACE_DIRS`, without following symlinks.
    - A trace whose own recorded events (`wallTime`) end before the turn started is rejected,
      whatever its file time says.
3. Judge each trace with `evidence-judge judge --offline`, in a child process. The child runs
   in a private temporary directory, so the project's `.env` cannot reach it, and inherits no
   `EVJ_*` variable except those in `EVJ_HOOK_PASS_ENV`. The judge gets the agent's final
   message as the answer, the last prompt as the task, and the spec saved at turn start (or
   the live spec when the turn-start hook did not run).
4. Decide:
    - **Any trace FAILs:** return `{"decision": "block", "reason": ...}`. The reason names the
      trace, the verdict's summary and up to five claims or spec expectations the evidence does
      not back up, with evidence ids.
    - **Bad evidence after Playwright was used:** no trace, or a trace that is missing,
      unreadable, older than the turn or refused by the judge. Block with instructions to record
      a trace (`EVJ_HOOK_ON_BAD_EVIDENCE=notice` only warns instead). Without Playwright use,
      bad evidence is a notice.
    - **The spec changed during the turn:** edited, deleted or created. Block, and say so
      (`EVJ_HOOK_BLOCK_ON_SPEC_CHANGE=false` only warns).
    - **Otherwise:** PASS and NEEDS REVIEW allow the stop with a one-line `systemMessage`.
5. **`stop_hook_active`:** Claude is already continuing because a stop hook blocked. The
   answer is still judged, but the hook never blocks twice in a row. If it would block, the
   stop goes through with a `systemMessage` that starts *"WARNING: the agent stopped again
   after being sent back, and its answer is still not backed by the evidence"*, followed by
   what is wrong. A second answer that passes gets the ordinary one-line PASS notice.

The hook fails open, with a notice, only for its own problems: bad hook input, invalid
`EVJ_HOOK_*` settings, a timeout or a judge crash.

Claude Code caps consecutive stop-hook continuations (8 by default,
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`), on top of the hook's own `stop_hook_active` guard.

## Configuration

Set these in the environment Claude Code runs in, or in the `env` block of a
[settings file](https://code.claude.com/docs/en/settings):

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
| `EVJ_HOOK_SESSION_SPEC` | unset | Session spec whose `rerun:` block the Stop hook runs, instead of `.evidence-judge/session.yaml`; see [Re-running the tests](#re-running-the-tests) |
| `EVJ_HOOK_PASS_ENV` | `[]` | `EVJ_*` variables the judge may inherit, a JSON list; every other `EVJ_*` is dropped |
| `EVJ_HOOK_MAX_TRACE_AGE_S` | `1800` | Newest trace age accepted when the turn's start is unknown |
| `EVJ_HOOK_LOG_DIR` | unset | Evidence-log directory when no `--log-dir` is given |
| `EVJ_HOOK_STATE_DIR` | `$CLAUDE_PLUGIN_DATA/state`, else `~/.evidence-judge/state` | The hooks' private state (spec snapshots, log key, log anchors) when no `--state-dir` is given |

These are read from the environment Claude Code starts the hooks with. A project `.env` file
is never read, by the hooks or by the judge they start.

The plugin's hook has a 90 second timeout, so the judge's own limit always fires first.

## Re-running the tests

The Stop hook can also check claims about tests, without trusting the test output the agent
saw. Add a session spec at `.evidence-judge/session.yaml` (or point `EVJ_HOOK_SESSION_SPEC` at
one outside the workspace) with a `rerun:` block:

```yaml
task: Fix the add() bug without touching the tests
rerun:
  command: [".venv/bin/python", "-m", "pytest", "-q"]
  timeout: 45
```

`claude-code-turn-start` saves this spec and the project's git revision when the turn starts.
At every `Stop` and `SubagentStop` whose saved spec has a `rerun:` block, after the browser
check, the hook runs `evidence-judge session judge --rerun` in the same isolated child as the
trace judge, with the session's evidence log (its key and anchor) and transcript. The judge
copies the project to a private directory, runs the command there without a shell in an
environment built from scratch, and reads the runner's own report
([details](../design/coding-agent-sessions.md#independent-re-run)). The agent's answer is then
judged against that result: "all tests pass" over a failing re-run is a FAIL, and Claude is sent
back with the failing tests named. Test-weakening edits the session made, and runner
configuration that differs from the turn's starting revision, are NEEDS REVIEW. A spec whose
`rerun:` block is removed or changed during the turn is a spec change, and blocks.

The re-run is opt-in because it **runs the whole suite at every stop**. Keep the suite fast, or
keep the re-run for projects where a false "tests pass" costs more than the wait. The re-run
must finish within `rerun.timeout` and within `EVJ_HOOK_TIMEOUT_S`, which also bounds the trace
judge, and the plugin's `Stop` hook is killed after 90 seconds; a longer suite needs your own
`Stop` hook entry with a longer `timeout` and a matching `EVJ_HOOK_TIMEOUT_S` (up to 600). A
re-run the hook could not finish allows the stop with a notice.

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
 "tool_use_id":"toolu_…","duration_ms":2310,"seq":4,"prev":"9f2c…",
 "chain":"hmac-sha256","key_id":"5be1…"}
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
- **Keyed chain and anchor.** `seq` counts lines from 0. `prev` is the HMAC-SHA256 of the
  previous line under a per-installation key (`${CLAUDE_PLUGIN_DATA}/state/log-key`, mode
  `0600`), and `key_id` names that key. After every line, the recorder writes the line count
  and the last line's MAC to a head anchor (`state/<session_id>/log-head.json`). An edit after
  the fact breaks the chain, and lines cut off the end no longer match the anchor. Lines are
  written under a lock with fsync; the directories are `0700` and the files `0600`.
- **Order.** The recorder hooks are `async`, so they never slow the agent down, but they can
  finish out of order. `seq` is assigned under the lock, in arrival order. `ts` is the hook's
  own clock and `tool_use_id` ties each line to the transcript.

`evidence-judge session judge --evidence-log` verifies the chain and the anchor. It still
reports the log as `partial` tamper evidence unless you pass `--trusted-log`: see below.

## Security

- **Offline by default.** The judge runs with `--offline` and without `ANTHROPIC_API_KEY` in
  its environment unless `EVJ_HOOK_LLM=true`. Nothing leaves the machine.
- **Nothing the agent wrote is executed.** The answer goes to the judge in a temporary file;
  the task travels as a single `--task=` argument, never through a shell.
- **Bounded.** Hook input over 8 MB is refused; trace search does not follow symlinks and
  stops at a depth of 5 and 20,000 entries; a packed trace is limited to 512 MB and only
  includes files inside its own directory.
- **Fails open only on its own problems.** Bad hook input, invalid settings, a timeout or a
  judge crash allow the stop with a one-line notice. Missing, junk or stale evidence after
  Playwright was used does not: the agent is sent back.
- **Settings the agent cannot reach.** The judge runs in a private temporary directory with
  `EVJ_*` variables dropped unless `EVJ_HOOK_PASS_ENV` lists them, so a `.env` the agent
  writes in the project changes nothing.
- **Untrusted page text.** The block reason may quote checker reasons that repeat page text.
  It tells Claude to treat that text as data, and evidence excerpts themselves are never
  forwarded.

## Keeping the hooks on

An agent running as your OS user can switch the hooks off: `{"disableAllHooks": true}` in
`.claude/settings.local.json` stops every user, project, local and plugin hook, so neither the
recorder nor the Stop judge runs again. Deleting or replacing the `evidence-judge` executable
has the same effect. Three layers stand in the way:

- **The guard hook.** The plugin's `PreToolUse` hook (`evidence-judge hook claude-code-guard`,
  on `Bash|Write|Edit|MultiEdit|NotebookEdit`) denies calls that write agent configuration
  (`.claude/settings*.json`, `.claude/hooks/`, `hooks/hooks.json`, `.mcp.json`, plugin
  directories, `~/.codex/`, `~/.gemini/`), the `evidence-judge` install (`~/.local/bin/`, the uv
  tool directory, `uv tool uninstall evidence-judge`) or the evidence logs. It reuses the
  session judge's shell analysis, takes about 0.3 s, and allows anything it cannot read. Set
  `EVJ_HOOK_GUARD=false` to turn it off.
- **A pinned CLI.** The first hook run pins the CLI it found (path and checksum) in
  `${CLAUDE_PLUGIN_DATA}/state/evidence-judge-pin`. If that file later disappears or changes,
  the Stop hook fails closed: it sends the agent back once (exit 2) and then stops the session
  instead of letting an unchecked answer through. After reinstalling the CLI somewhere else,
  delete the pin file; `EVJ_HOOK_ALLOW_MISSING_JUDGE=1` in Claude Code's own environment brings
  back the old notice.
- **The session judge.** A session that writes agent configuration or the judge's install
  anyway, say from a script the guard cannot see into, goes to review as harness tampering.

None of this holds against an agent that can run arbitrary code as you: it could rewrite the
plugin itself. The control that does hold is managed settings, which the agent cannot write
without administrator rights. `disableAllHooks` set anywhere else does not stop managed hooks
or hooks of plugins force-enabled in managed `enabledPlugins`, and `allowManagedHooksOnly`
(managed settings only) drops every user, project and other plugin hook
([settings reference](https://code.claude.com/docs/en/settings-reference#disableallhooks),
[managed settings](https://code.claude.com/docs/en/managed-settings)). In
`managed-settings.json`:

```json
{
  "enabledPlugins": { "evidence-judge@evidence-judge": true },
  "allowManagedHooksOnly": true,
  "permissions": {
    "deny": [
      "Edit(~/.claude/settings*.json)", "Edit(./.claude/**)", "Edit(~/.local/bin/evidence-judge)"
    ]
  }
}
```

## What the hooks can and cannot guarantee

The hooks check the agent's answer against evidence. They are only as strong as the agent's
inability to rewrite that evidence.

- **Everything in the workspace is agent-writable.** Traces under `.playwright-mcp/`, the spec
  at `.evidence-judge/spec.yaml` and any `.env` are files the agent can change. The hooks
  defend against the obvious moves: a spec changed mid-turn, a planted or junk trace, deleted
  traces, a project `.env`. They cannot tell a careful forgery of a whole trace from a real
  one if the agent can write both.
- **The state directory is only private if the agent cannot reach it.** The spec snapshot, the
  log key and the log anchors live in `${CLAUDE_PLUGIN_DATA}/state`, outside the workspace. An
  agent running unsandboxed as your OS user can still read and write them, sign a forged
  evidence log with the key, and update the anchor. That is why `session judge` counts a
  verified log as `full` tamper evidence only with `--trusted-log`.
- **To make the guarantees hold, keep the agent out of the state directory:**
    - Run Claude Code with its sandbox on and deny the agent the hooks' directories (below).
      The hooks run outside the sandbox, as part of Claude Code itself.
    - Or run the agent in a container or VM, or as another OS user, that does not mount
      `~/.claude/plugins/data/` or `~/.evidence-judge/`.
    - Put the spec outside the workspace with `EVJ_HOOK_SPEC=/absolute/path/spec.yaml`.

In `~/.claude/settings.json` (or managed settings, so a project cannot loosen it):

```json
{
  "sandbox": {
    "enabled": true,
    "allowUnsandboxedCommands": false,
    "failIfUnavailable": true,
    "filesystem": {
      "denyRead": ["~/.evidence-judge", "~/.claude/plugins/data"],
      "denyWrite": ["~/.evidence-judge", "~/.claude/plugins/data"]
    }
  },
  "permissions": {
    "deny": [
      "Read(~/.evidence-judge/**)", "Edit(~/.evidence-judge/**)",
      "Read(~/.claude/plugins/data/**)", "Edit(~/.claude/plugins/data/**)"
    ]
  }
}
```

- `sandbox.filesystem.denyRead` / `denyWrite` confine every command the agent runs through
  Bash, including scripts.
- The `Read` / `Edit` deny rules cover Claude's own file tools.
- `allowUnsandboxedCommands: false` removes the `dangerouslyDisableSandbox` escape hatch, and
  `failIfUnavailable: true` stops commands from running unsandboxed when the sandbox cannot
  start.
- Commands in `sandbox.excludedCommands` run outside the sandbox, so keep that list empty.

With this in place, a verified evidence log is fit for `session judge --trusted-log`.
([sandboxing](https://code.claude.com/docs/en/sandboxing),
[settings](https://code.claude.com/docs/en/settings-reference),
[permissions](https://code.claude.com/docs/en/permissions))

## Limits

- The hook reads the transcript for the turn's start time, the traces `browser_stop_tracing`
  reported and, as a fallback, the final message. Claude Code documents the transcript format as internal, so if a release changes it,
  the hook falls back to the 30 minute window rather than failing.
- A turn that browsed without tracing is sent back to record a trace
  (`EVJ_HOOK_ON_BAD_EVIDENCE=notice` only warns).
- Playwright use is detected from Playwright MCP tool calls in the transcript. A browser the
  agent drives some other way (a script it runs, another MCP server) is judged only if it left
  a trace in the trace directories.
- A `SubagentStop` and the parent's `Stop` can judge the same trace, each against its own
  final message.

Sources: [hooks reference](https://code.claude.com/docs/en/hooks),
[plugins reference](https://code.claude.com/docs/en/plugins/manifest-reference),
[marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference),
[skills](https://code.claude.com/docs/en/skills),
[Playwright MCP](https://github.com/microsoft/playwright-mcp).
