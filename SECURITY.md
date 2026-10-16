# Security

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/hsayvaidya23/evidence-judge/security/advisories/new),
not as public issues. Include the version (`evidence-judge version`), a description, and a
minimal reproduction **with all real data removed**. You can expect an acknowledgement within
five working days.

Supported versions: the latest release. v0.1.0 is not released yet, so for now that is the `main` branch.

## What Evidence Judge handles

Agent recordings are sensitive. A Playwright trace or HAR file routinely contains session
cookies, bearer tokens, API keys, form submissions (including passwords), personal data and
payment details. Evidence Judge reads these files locally and, when an LLM is configured, sends
selected, redacted evidence text to the model provider. Screenshots are only sent when you opt in
with `EVJ_SCREENSHOTS_TO_LLM=true` (see Limits).

## Redaction

Redaction is **on by default** (`EVJ_REDACT=true`). It runs immediately after ingest, before
claim extraction, before any LLM call and before any report is written. It covers:

| Category | What is redacted |
| --- | --- |
| `auth` / `bearer` / `jwt` | `Authorization` and `Proxy-Authorization` credentials (also written in free text, as in `Authorization: Token 9f8e...`), `Basic` credentials (base64 of `user:password`), bearer tokens and JWTs anywhere |
| `cookie` | Every value in `Cookie` / `Set-Cookie` (cookie names are kept) |
| `secret_header` | `X-API-Key`, `X-Auth-Token` and headers whose names contain token, secret, session, auth, password, api-key |
| `api_key` | Common key formats: `sk-...`, `ghp_...`, `github_pat_...`, `AKIA...`, `xox[bp]-...`, `AIza...`, Stripe keys, GitLab tokens |
| `secret_param` | The value of a URL query, fragment, path (`;jsessionid=`), form or JSON field (string or number) whose name is a credential, session id or payment/identity secret: `password`, `passwd`, `pwd`, `pass`, `pw`, `passcode`, `passphrase`, `secret`, `token` (and names ending in `token`, `secret`, `password`), `api_key`, `apikey`, `client_secret`, `access_token`, `refresh_token`, `id_token`, `session_id`, `sessionid`, `jsessionid`, `phpsessid`, `aspsessionid`, `csrf`, `xsrf`, `otp`, `otp_code`, `totp`, `mfa`, `2fa`, `two_factor_code`, `recovery_code`, `backup_code`, `pin`, `cvv`, `cvv2`, `cvc`, `cvc2`, `csc`, `cvn`, `security_code`, `verification_code`, `one_time_code`, `ssn`, `social_security_number`, AWS and Google signature fields; names are split into words, so `card[cvc]`, `cardCvv`, `twoFactorCode` and `payment.pin` count, but `shipping` or `passenger` do not. In URLs also `key`, `code`, `session`, `sig`, `signature`, `auth`, `sid`, including URLs written inside text, JSON strings (escaped `\/` too), console messages, bodies and the agent's answer. Free-text pairs such as `password: hunter2` or `api_key = 'ab12...'` count when the value has a digit or symbol (or is 20+ characters), so prose like "Password: required" or "password reset link sent" is left alone; `pass:` and `pw:` count only in `=`-pairs, forms and JSON |
| `phone` | Phone numbers (7 to 15 digits, with `+ ( ) - .` and spaces) in a JSON, form or query field named for one: `phone`, `phone_number`, `mobile`, `tel`, `telephone`, `cell`, `msisdn`, `fax`. Phone-shaped numbers anywhere else are left alone |
| `ssn` | US Social Security numbers written `###-##-####` (valid area, group and serial) when `SSN` or `social security` appears just before them or the field is named for one |
| `url_password` | Passwords in `user:password@host` URLs |
| `secret_input` | Values typed into fields whose selector suggests a password, OTP, PIN or card security code |
| `html_secret` | The `value` of an `<input>` in page or body HTML that is `type="password"`, has a password or one-time-code `autocomplete`, or whose `name`/`id` contains a password, token, secret, OTP, PIN, CVV or CSRF word (hidden CSRF/auth tokens included) |
| `email` | Email addresses |
| `card` | Payment card numbers (issuer prefix + Luhn checked) written in card groups (`4111 1111 1111 1111`), or as bare digits when the field name (`card_number`, `pan`, `cc`) or nearby words (card, payment, visa, ...) say it is a card. A card number seen once in a run is redacted wherever it repeats. Bare numbers in order, tracking or reference id fields are left alone |

This applies to request and response bodies, headers, URLs, page snapshot text and HTML, console
messages, action values, run metadata, and the agent's answer and reasoning. Percent-encoded
values (`jane.doe%40example.com` in a form body, URL path or query) are decoded, checked and
re-encoded. User-supplied task specs and claim files are redacted the same way so their
predicates still match.

Each value becomes `[REDACTED:<category>:<fingerprint>]`, where the fingerprint is the first
8 lowercase hex characters of an HMAC-SHA256 of the value's canonical form: emails are
case-folded and card and phone numbers reduced to their digits, because evidence matching ignores case and
card formatting; credentials are fingerprinted exactly. Equal values get equal tokens, so a claim
that quotes an email still matches the request that sent it, without the email ever being shown,
and redaction never changes a verdict (this is tested). Set `EVJ_REDACTION_KEY` to keep
fingerprints stable everywhere; otherwise, when a run uses the LLM cache, a random key is kept in
`<EVJ_CACHE_DIR>/redaction.key` (mode 0600), and in every other case a random key is used per
process. Treat the key and the key file as secrets: with the key, a fingerprint can be confirmed
by guessing.

### Limits

- **Screenshot pixels are never redacted.** Redaction works on text; a screenshot shows
  whatever was on screen, including typed passwords left visible, personal data and card
  numbers. Screenshots are therefore **not sent to the model provider by default**
  (`EVJ_SCREENSHOTS_TO_LLM=false`; screenshot checks then report no evidence). If you set it to
  `true`, `judge` prints a notice and a claim that reaches the screenshot check sends the
  screenshot, unchanged, to the vision model. HTML reports always embed screenshots unchanged.
  To keep sensitive pixels out:
  - Record with sensitive fields masked. For explicit Playwright screenshots pass
    `mask=[locator, ...]` (and `style=` to hide elements); trace screencast frames cannot be
    masked, so hide sensitive elements with injected CSS (`page.add_style_tag`) or record the
    trace with `tracing.start(screenshots=False)`.
  - Run agents against test accounts and synthetic data when the recording will be judged with
    a hosted model or the report will be shared.
  - Use `--offline` to judge without any model call, so no screenshot leaves the machine, and
    treat HTML reports from real sessions as sensitive.
- Redaction is pattern-based. Secrets in unusual formats (custom tokens, secrets in fields
  with unrecognised names), names, postal addresses, dates of birth, non-US national ids and
  phone numbers outside phone-named fields pass through. Review reports before sharing them.
- `--no-redact` (or `EVJ_REDACT=false`) disables all of the above. Use it only on synthetic data.

## Trust assumptions

Evidence Judge checks an agent's claims against a recording. It cannot check the recording
itself, so a verdict is only as trustworthy as whatever made the evidence:

- **The recorder is trusted.** The harness that starts the browser and records the run is
  assumed honest. An agent that can write or edit its own recording can make it say anything.
- **A HAR file or a browser-use history is only as honest as its producer.** A HAR is a JSON
  file of requests and responses; a hand-written one that shows `POST /api/orders` answered
  `201` is indistinguishable from a recorded one and can score `PASS`. A browser-use history is
  written by the agent framework itself.
- **State from before recording started is invisible.** A persistent browser profile, cookies,
  local storage, IndexedDB, service workers, extensions or a proxy set up before tracing began
  can change what the site shows or what requests do, and no source records it.
- **A trace covers one browser context.** An agent that holds the `Browser` or the
  `Playwright` object can open a second context or an `APIRequestContext`
  (`playwright.request.new_context()`) and send requests through it, such as the upgrade or
  order a `forbid` entry rules out; nothing of that context is in the trace, and the traced
  pages then show the result as if the site had done it unasked. Give the agent only the
  context the harness created and traces (see
  [harness setup](docs/task-specs.md#harness-setup-give-the-agent-only-the-traced-context)).

What each source can and cannot reveal about the agent tampering with the page or its requests
is reported in every browser-run verdict as `tamper_detection` (`level` and `reason`), with a
warning when it is not `full`. A `session judge` verdict carries `tamper_detection` for the
session record plus `verification` (where its test results come from); a `session rerun`
verdict has no session record, so its `tamper_detection` is `null` and only `verification` is
set.

| Source | `tamper_detection` | Can reveal | Cannot reveal |
| --- | --- | --- | --- |
| Playwright trace with a HAR that added requests or filled fields | `partial` | As for a trace; what only the HAR holds still contradicts claims and shows side effects | Whether what only the HAR holds happened: it never proves a claim, an `expect` entry or a constraint |
| Playwright trace | `full` | Every call made through Playwright on the traced browser context: injected scripts (`evaluate`, `add_init_script`, `set_content`, ...), route handlers that answer or rewrite requests, storage and cookie writes, CDP sessions (deny by default) | Calls on any other context: a second `browser.new_context()`, or `playwright.request` / an `APIRequestContext`, whose requests and pages are not in the trace; what a CDP session then sent; anything done outside Playwright (another process driving the browser, a proxy, OS-level input); state from before tracing started |
| browser-use history | `partial` | The model's actions, including its `evaluate`; any action that is not a built-in tool, or that carries code, counts as running the agent's code | Scripts, CDP calls and request changes beneath those actions; network evidence unless a HAR is added |
| HAR alone | `none` | Nothing about the agent's calls | Whether the HAR was recorded at all |

For gating CI on verdicts:

- Record a **Playwright trace from the harness**, not from inside the agent, and keep the agent
  from writing the trace file. Trace the context the agent works in and give the agent only
  that context or its page, never the `Browser`, the `Playwright` object or
  `playwright.request`.
- Pass **`--require-tamper-evidence`** (or set `require_tamper_evidence: true` in the task
  spec): a run whose `tamper_detection` is not `full` is then `NEEDS_REVIEW` at best. That
  excludes a trace merged with a HAR that added requests or filled in fields the trace recorded
  empty; judge such a trace without its HAR.
- Start each run from a fresh browser context, without a persistent profile; pass any login
  state with `storage_state=` when the context is created.
- Treat a `PASS` on a HAR-only or browser-use run as "the recording is consistent with the
  claim", not as proof that the run happened.

## Agent hooks and evidence logs

The coding-agent hooks (Claude Code, Codex, Gemini CLI) and their evidence logs run beside an
agent that can usually read and write files as your OS user. The threat is that agent
weakening the check of its own answer.

| Attack | What the hooks do |
| --- | --- |
| Stop again after being sent back, hoping the second answer is not checked | The answer is judged again. The hook does not block twice, to avoid a loop, but the user gets a warning that names what is wrong |
| Edit or delete `.evidence-judge/spec.yaml` during the turn | The turn-start hook saved the spec, outside the workspace, before the agent acted. The judge uses that copy, and the agent is sent back for changing the spec |
| Write a junk trace, so the judge refuses it and the hook fails open | A trace the judge refuses is bad evidence. After Playwright use, the agent is sent back |
| Plant an older clean trace with a fresh mtime | Every trace of the turn is judged, not just the newest. A trace whose own recorded `wallTime` predates the turn is rejected. When `browser_stop_tracing` reported trace paths, only those count |
| Delete the traces | Playwright use with no trace sends the agent back |
| Write a `.env` in the project to change the judge's settings | The judge runs in a private temporary directory, with `EVJ_*` variables dropped unless the hook config passes them on |
| Rewrite the evidence log and recompute its chain, or cut lines off its end | Lines are HMAC-chained under a per-installation key, mode `0600` in the hooks' state directory. A head anchor there records the line count and last MAC |
| Switch the hooks off: `{"disableAllHooks": true}` in `.claude/settings.local.json`, an edited `hooks.json`, `hooksConfig.enabled: false` | The guard hook (`PreToolUse` / `BeforeTool`) denies writes to agent configuration, hook files and evidence logs; a session that gets one through anyway goes to review as harness tampering |
| Delete, uninstall or shim the `evidence-judge` CLI so the hook shim finds nothing | The guard denies it; the shim pins the CLI's path and checksum on first use and the Stop hook fails closed when either changes; the session judge reports it as harness tampering |

Three limits remain, and the verdicts say so:

- **The key and the state directory are only private if the agent cannot reach them.** An
  unsandboxed agent running as your user can read the key, sign a forged log and update the
  anchor; it can also forge a whole trace. So `session judge` reports a verified evidence log
  as `partial` tamper evidence unless you pass `--trusted-log` (or set `trusted_evidence_log:
  true` in the session spec). Pass it only when the log, key and anchor are out of the agent's
  reach. Three ways to get there:
    - Claude Code's sandbox with `sandbox.filesystem.denyRead`/`denyWrite` on
      `~/.evidence-judge` and `~/.claude/plugins/data`, plus `allowUnsandboxedCommands: false`.
    - Codex `workspace-write` together with a separate OS user or container, since that
      sandbox confines writes but not reads.
    - Running the agent in a container or VM that does not mount those directories.

  The [Claude Code guide](docs/guides/claude-code.md#what-the-hooks-can-and-cannot-guarantee)
  has the settings.
- **A same-user agent can still turn the hooks off.** The guard decides from each call's text,
  so code it cannot see into (a compiled program, a script written earlier) can still edit
  settings, the plugin or the shim itself. Only configuration the agent cannot write closes
  this: Claude Code managed settings (`allowManagedHooksOnly`, the plugin force-enabled in
  managed `enabledPlugins`; `disableAllHooks` outside managed settings does not stop those),
  Codex's managed `requirements.toml`, or an agent sandboxed away from its own configuration.
  The [Claude Code guide](docs/guides/claude-code.md#keeping-the-hooks-on) has the settings.
- **Fail-open is limited to the hooks' own problems.** Bad hook input, invalid settings, a
  timeout or a judge crash let the stop through with a notice, so a broken install never traps
  the agent. Gemini CLI's hook input does not show which tools ran, so there missing or junk
  traces are only a notice.

## Re-running the tests

`session judge --rerun`, `session rerun` and the stop hooks with a session spec's `rerun:` block
run the project's test suite. What they execute, and what they protect against:

- **The spec author chooses the command, never the agent.** The command is an argv list in the
  session spec, which the hooks snapshot outside the workspace when the turn starts; a spec
  changed during the turn is reported and the snapshot is used. Keep the spec out of the
  agent's reach (`EVJ_HOOK_SESSION_SPEC=/absolute/path`) when the verdict matters.
- **It runs the project's own test code.** That is the same trust as running the suite
  yourself: code the agent wrote runs as your user. The run happens on a copy of the working
  tree in a temporary directory, without a shell, in an environment built from scratch (a
  judge `HOME` and `TMPDIR`, the judge's `PATH` without relative entries, `LANG`, toolchain
  cache locations and the spec's `env`), so rc files, `BASH_ENV`, aliases, exported variables,
  `PYTEST_ADDOPTS`, `GOFLAGS`, `NODE_OPTIONS` and `~/.npmrc` do not apply. Python's bytecode
  goes to a judge directory (`PYTHONPYCACHEPREFIX`), so no `__pycache__` file planted next to
  a source is loaded. It is not a sandbox: the code can still read and write anything your
  user can, use the network, and reach the dependency directories (`.venv`, `node_modules`),
  which are linked into the copy.
- **What is copied is decided by the project.** The file list comes from git, but not from
  state anything running as you can change on the side: `.git/info/exclude` and
  `core.excludesFile` are not read, files tracked at the base revision are copied whenever
  present, untracked paths that only `.gitignore` rules added since the base ignore are copied
  anyway, a removed `!exception` is a configuration change, and a `core.worktree` pointing
  elsewhere copies the whole directory and needs review. A file write into `.git/` in the
  session record is a harness change.
- **What the run can read from outside the copy.** The copy sits in the system temporary
  directory, so configuration above your repository (`pytest.ini`, `tox.ini`, `setup.cfg`,
  `pyproject.toml`) is not above it; runner configuration found in the directories that are
  above it (pytest ini files and `conftest.py`, `go.mod`/`go.work`, `package.json`,
  `node_modules`, `Cargo.toml`, `.cargo`) needs review. The linked `.venv`, `venv` and
  `node_modules` are read from your tree, as are an editable install's sources (its `.pth`
  points at your `src/`), absolute symlinks in the tree and the toolchain caches. The
  turn-start hook fingerprints the dependency directories (every file's size and times in a
  virtualenv, skipping `__pycache__`; the top-level package manifests and `.bin` of
  `node_modules`) and the re-run fingerprints them again: any difference needs review. Without
  the hooks (the plain CLI) there is no fingerprint, and the session record's install and
  harness findings are what remains. The virtualenv's base interpreter and standard library,
  and edits inside a `node_modules` package other than its manifest, are not fingerprinted.
- **The code under test can still lie, within limits.** The verdict comes from the runner's
  machine-readable report, written to its own private temporary directory (mode 0700, a random
  name not derived from the copy's path), never from stdout: a summary printed at exit or an
  exit code forced with `os._exit(0)` does not change it, and a report with failures fails.
  The code under test runs as your user, in the same process as the runner that writes the
  report, and the report path is in its own `argv`: it can in principle find the report and
  replace it with a forged one. A forgery that leaves the report inconsistent with itself (test
  cases against stated totals) is needs review; a careful, self-consistent one is not detected.
  A source change that does this is a runner patch the transcript analysis reports, so such a
  run still needs review when the session record shows it.
- **The hardening path is an OS sandbox.** `rerun.sandbox` takes an argv prefix the judge runs
  the command under (`bwrap`, `sandbox-exec`, `docker run ...`), with `{tree}`, `{report_dir}`,
  `{home}` and `{tmp}` filled in, so you can keep the run off your files, the hooks' state and
  the network. Because the runner writes the report from inside the same sandbox as the code
  under test, a sandbox protects your machine, not the report's integrity. Run untrusted code
  in a container or VM.
- **Weakened tests stay weakened.** A re-run of a suite whose assertions were removed passes.
  Edits to tests, test data, snapshots and runner configuration, and source that patches the
  runner, are still found in the session record, and runner configuration in the copy is also
  compared with the base revision; either keeps the run at needs review.
- **Bounded.** The copy is limited to 200,000 files and 2 GiB, the run to the spec's timeout
  (its whole process group is killed), and the report to 64 MB; a report that declares a DTD or
  entities is refused.

For gating on coding-agent sessions, trust a "tests pass" only when the verdict's
`verification.method` is `rerun`: the judge ran the suite and read the runner's report. With
`transcript`, the result is what the session record says the agent's own test run printed.
Pass `--require-rerun` so that a session without a re-run cannot pass, and when the agent is
adversarial, run the suite under `rerun.sandbox` (or in a container or VM) so that the code
under test cannot reach your files or the hooks' state.

Without a re-run, how many tests a session left failing, and which, is read from its own test
output, and only from a whole-suite run that ran to the end. A run through a wrapper (`make
test`, `npm test`, `tox`) counts as whole and run to the end only when the session shows what
the wrapper ran: the wrapper's own echo of its command (npm's `> jest` header, tox's
`commands[0]>` line, make's echo of a recipe line) or a `Makefile` or `package.json` the session
read whole; otherwise its pass or fail still counts, but it backs no count of failing tests and
no "`test_x` is not among the failures". Two gaps remain there. An unseen wrapper's green run
is still taken as the whole suite, so a filter inside a silent recipe can make a partial pass
look whole, and variables a Makefile or tox environment exports (`PYTEST_ADDOPTS`) are not
seen through the echo. And a claimed fix that admits failures without counting or naming them
passes when the tests failing at the end are a strict subset of those failing before the first
change, a comparison that trusts the test names the runs printed. With `--require-rerun`, every
count and the end of that comparison come from the judge's own run of the whole suite.

Lint and type-check claims are read from the session record only; a re-run does not repeat
them. The judge reads the tool's own summary and its options, and through a wrapper (`npm run
lint`, `make lint`) the command the wrapper's echo or definition shows, so a recipe running one
file or `--exit-zero` cannot back a clean claim; a wrapper neither shows (`tox -e lint`, `just
lint`, a silent recipe) is still judged by its exit status alone. `pre-commit run` decides the
project only with `--all-files` and no `SKIP`, though a `SKIP` exported by an earlier command
is not seen. A suppression comment written by a shell command is read from the text the command
line spells out (`sed -i` and `perl -pi` substitutions, heredocs, `echo`), or from the whole
command line when it writes a source file the judge cannot follow (`python -c`); one a script
file writes is not seen.

Claims the session judge has no predicate for are found by a closed list of actions whose
effects only a command could show (version control, installs, deployments, releases, pull
requests, migrations) and by coverage and speed figures; an unverifiable claim in other words
("I notified the team", "the docs site is updated") is not flagged by the offline extractor. An
install claim must name the package on the install command line, so an honest "Installed
`requests`" after `pip install -r requirements.txt` fails. "Pushed to origin/main" is checked
against the remote a successful `git push` names and the branches git's own ref lines (or its
refspecs) show: a push elsewhere contradicts it, and one that shows neither is no evidence. A
bare `git push` is taken to go to `origin`, so a branch whose upstream is another remote is not
caught.

The MCP server applies the same rule to what it returns. Page- and agent-derived text is
fenced in `<untrusted_data>` tags. `judge_run` never replaces a file that is not an earlier
evidence-judge report unless the caller passes `overwrite: true`.

## Untrusted recordings

Recordings are treated as untrusted input, since they may come from a third party or a
compromised agent:

- **File references stay inside the recording.** A browser-use `screenshot_path` and a HAR
  `_file` body are read only when they resolve, after following symlinks, inside the history or
  HAR directory (for browser-use, or an explicit `screenshot_root`). Screenshots must also be
  regular files of at most 10 MB that start with PNG, JPEG, WebP or GIF magic bytes. Anything
  else is skipped with a warning in the run metadata.
- **Trace archives are size-checked before they are inflated.** Using the sizes the zip
  declares (which `zipfile` never inflates past), a trace fails with `TraceTooLargeError` when
  an event file exceeds 200 MB, the archive exceeds 1 GB uncompressed or 100,000 entries, or an
  entry over 1 MiB compresses more than 200:1. Bodies and screenshots over 10 MB are skipped
  with a warning. Pass `limits=TraceLimits(...)` to `load_playwright_trace` to change these.
- **HAR and browser-use files are size-capped.** A HAR over 200 MB or a browser-use history over
  50 MB fails with `TooLargeError` without being read past the cap (`max_file_bytes=` on
  `load_har` / `load_browser_use_history` changes this). `TraceTooLargeError` is a
  `TooLargeError` too.
- **Memory stays bounded.** Event files are streamed line by line, only the part of a body that
  `max_body_chars` keeps is read, and trace screenshots are read from the zip on demand.
- **Mismatched evidence is refused.** Loading a trace with a HAR fails with
  `MismatchedEvidenceError` instead of merging another run's traffic into the evidence when the
  HAR shares none of a small trace's requests (fewer than 5), or fewer than half of a larger
  trace's; when fewer than half of the HAR's entries pair with a trace request or, if both
  recordings carry wall-clock times, start within 2 seconds of the trace's time window; or when
  the two time windows do not overlap at all. Requests only the HAR saw are listed in
  `metadata["har_only_requests"]` and marked `har_only`; fields of a trace request the trace
  recorded empty and the HAR filled in are named in its `har_filled`. Neither proves a claim, an
  `expect` entry or a constraint (the predicate must hold on the trace alone), though both can
  contradict one and show a side effect. HAR-only `POST`/`PUT`/`PATCH`/`DELETE` requests are dropped,
  so a side effect the trace never recorded cannot satisfy an expectation on the HAR's word
  alone, and HAR-only requests starting more than 10 minutes outside the trace's time window are
  dropped too; both are noted in `metadata["merge_warnings"]`. `allow_mismatch=True` (CLI
  `--allow-mismatch`) overrides all of this.
- Every ingest failure is an `IngestError` (a `ValueError`), reported by the CLI as bad input
  (exit code 3).

## Rules for contributors and users

- **Never commit traces, HAR files or screenshots that contain real secrets or personal data.**
  Fixtures must be synthetic. `.gitignore` excludes `out/` and `.evidence-judge/`; keep
  recordings there. The LLM cache and redaction key live outside the repository, in the per-user
  cache directory (`EVJ_CACHE_DIR`).
- Never commit `.env`. API keys are read from the environment only.
- Third-party GitHub Actions in the workflows and `action.yml` are pinned to full commit SHAs
  with a `# vX.Y.Z` comment (Dependabot keeps both current); a new one must be pinned the same
  way, which a unit test checks.
- Agent output is untrusted input. The HTML report escapes all agent-controlled text and ships a
  Content-Security-Policy that blocks scripts and external requests; console output never
  interprets agent text as markup. Treat any way around this as a vulnerability.
