# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). What SemVer covers (the CLI, the verdict JSON and
the names exported from `evidence_judge`) is defined under
[Stability](docs/reference/python-api.md#stability).

## [Unreleased]

### Added

- Ingest of Playwright traces (format v7–v10), HAR 1.2 and browser-use histories (a history file
  or a run directory) into one normalised `Run`; a HAR recorded with a trace is merged into it.
- Claim extraction with Claude (structured output, verbatim-quote validation, prompt-injection
  defences) and an offline heuristic extractor. Offline, "changed the X to Y" becomes a claim
  proven by the `POST`, `PUT` or `PATCH` that carried Y.
- Typed predicates (`request`, `page_text`, `url`, `action`, `value`, `screenshot`) with
  deterministic checkers; the screenshot vision check runs only as a last resort.
- Task specs with `expect`, `forbid`, `constraints`, `allow`, `site` and `allowed_hosts`,
  validated when they load (statuses, regexes, JSON paths, numbers, hosts). `request` predicates
  gain `query_contains`; `allowed_hosts` takes `*.` wildcards for subdomains.
- `failure_markers` in a spec: regexes, in any language, for failure pages a redirect can land
  on.
- `trusted_scripts` in a spec: the sha256 of harness scripts you vouch for, so they do not
  count as the agent tampering with the page.
- Six verdict dimensions: success, side effects, state, guardrails, recovery and loops.
- PASS / FAIL / NEEDS_REVIEW outcomes; evidence absence counts only on captured channels.
- Secret and PII redaction with HMAC fingerprints, so redacted values still match, and a stable
  per-installation redaction key, so LLM cache hits survive restarts.
- JSON (versioned schema `evidence-judge/run-verdict@1`), self-contained HTML and console
  reports. The HTML report meets WCAG AA contrast and works on phones.
- `evidence-judge` CLI (`judge`, `inspect`, `bench`, `schema`, `version`) with CI-friendly exit
  codes; `judge --site` names the site the task is on. `inspect` lists every call that made
  evidence the agent's own, with each script's sha256 for `trusted_scripts`.
- A public Python API exported from `evidence_judge` (`Judge`, `JudgeOptions`, `load_run`,
  `TaskSpec`, `RunVerdict`, `Outcome`, `Settings`, `build_llm`, `Redactor`).
- LLM cost budget per judged run, concurrency cap, disk cache and a fake client for tests.
- Integrations (optional extras): OpenTelemetry `gen_ai.evaluation.result` events, Langfuse
  scores, Arize Phoenix span annotations and an Inspect AI scorer; `judge --otel` and
  `--langfuse-trace-id` export from the CLI.
- Docker image (non-root, multi-stage, base images pinned by digest, dependencies installed from
  the lockfile with hashes), built and smoke-tested in CI.
- Reusable GitHub Action with job-summary output and `outcome`, `score` and `report` outputs;
  pin it to a release tag or commit SHA.
- Demo shop and seven recorded agent runs with a golden regression test, plus real Chromium
  traces of agents that make their own evidence.
- Forgery lab: deterministic and LLM forgers for the four manipulation kinds from "Gaming the
  Judge", a style-only control, and evidence attacks: injected predicates, echoes of the agent's
  input, off-site confirmations, a mirror on another port, response echoes, script injection,
  mocked responses, links on agent-made pages, storage tampering and CDP or WebSocket tampering.
- `evidence-judge bench`: false-PASS rates with Wilson intervals for Evidence Judge (with a task
  spec, a generic spec and none) and a transcript LLM-judge baseline, a read-only task scenario
  and honest-edit controls with false REVIEW/FAIL rates.
- Documentation site: getting started, concepts, a spec cookbook (payment providers, settings
  changes, telemetry), CI, Docker, benchmarking and recording guides, and reference pages.
- `evidence-judge mcp`: an MCP server (the `mcp` extra) with `judge_run`, `inspect_run`,
  `explain_verdict`, `validate_spec` and `find_recordings` tools, each with typed input and
  output schemas, for Claude Code, Codex CLI, Gemini CLI and any MCP client. Stdio by default,
  streamable HTTP on `127.0.0.1` with `--http`. Files are read and written only inside the
  `--root` directories (symlinks resolved), judging is offline unless a call asks for a model
  the server has (`--offline-only` refuses it), and redaction follows `EVJ_REDACT`.
  `validate_spec` reports each spec error with its line, and `find_recordings` also lists the
  unzipped traces Playwright MCP writes.

- Every verdict says how much tampering its source could reveal (`tamper_detection`: `full`
  for a Playwright trace, `partial` for a browser-use history, `none` for a HAR alone) and warns
  when it is not `full`; `--require-tamper-evidence` (or the spec's `require_tamper_evidence`)
  caps such runs at `NEEDS_REVIEW`.
- LLM backends for the coding-agent CLI you are already logged into: `EVJ_LLM_BACKEND` (or
  `--llm` on `judge` and `bench`) picks `anthropic` (the default), `claude-code`, `codex` or
  `gemini`. Each call runs the CLI headless with every tool, MCP server, hook and plugin off, in
  an empty temporary directory, with the prompt on stdin and the reply validated against the
  schema; Claude Code's reported cost is charged to the run budget. Screenshots reach Claude Code
  and Codex; with Gemini, screenshot checks report no evidence. `EVJ_CLI_MODEL_FAST`,
  `EVJ_CLI_MODEL_STRONG` and `EVJ_CLI_TIMEOUT_S` configure them. See the LLM backends guide.
- `evidence-judge hook`: stop hooks for coding agents (`claude-code-stop`, `codex-stop`,
  `gemini-after-agent`). When a Playwright trace was recorded during the turn, they judge it
  offline against the agent's final message and keep the agent working on FAIL (Claude Code and
  Codex `decision: block`, Gemini `deny`); NEEDS_REVIEW blocks only with
  `EVJ_HOOK_BLOCK_ON_NEEDS_REVIEW`. They respect `stop_hook_active`, time out after 60 seconds
  and fail open with a one-line notice. Unzipped Playwright MCP traces are packed before judging.
- Evidence recorder hooks (`claude-code-record`, `codex-record`, `gemini-record`): one redacted,
  bounded, hash-chained JSON line per prompt, tool call and stop in a per-session log outside
  the workspace (log format version 1), with exit codes taken only from what the hook payload
  states.
- A Claude Code plugin and marketplace (`/plugin marketplace add hsayvaidya23/evidence-judge`):
  the stop and recorder hooks, the `evidence-judge mcp` server and a skill on recording traces
  with Playwright MCP, writing a spec and reading a verdict. A Gemini CLI extension with the
  same hooks and server, and guides for Claude Code, Codex CLI and Gemini CLI.
- Coding-agent sessions: `evidence-judge session judge` grades a Claude Code transcript (a path,
  a session id or `latest` for a project), a Codex `exec --json` stream or rollout, or an
  evidence-recorder log against its tool-call log, and `session inspect` lists what the judge
  sees in it. Claims ("all tests pass", "I fixed the bug in `x.py`", "the build succeeds", "I
  didn't touch the tests", "I ran the linter") compile to `test_result`, `command`, `edit` and
  `build` predicates checked deterministically; session specs add `expect` and `forbid`
  predicates. Design and formats: `docs/design/coding-agent-sessions.md`.
- A passing test run proves a pass only when it ran after the last workspace change, without
  filters, with a parsed runner summary (pytest, unittest, jest, vitest, mocha, go, cargo, rspec,
  phpunit, dotnet, maven), its own exit status and its whole output, from a command line that
  does not print summary text itself, and with no test-weakening change in the session: skip
  markers, removed or rewritten assertions, deleted tests, test configuration, conftest hooks,
  runner shims, patched packages, aliases, shell functions or PATH changes.
- Evidence-recorder log format v1 (`--evidence-log`), shared by Claude Code, Codex and Gemini
  hooks, with an optional hash chain; such logs and saved harness streams give
  `tamper_detection: full`, transcripts `partial`.
- A session forgery benchmark (`python -m evidence_judge.sessions.bench`): 253 forged successes
  over real Claude Code sessions, rewritten answers and 40 tricks, with no false PASS.

### Changed

- A run passes only when the site is named: the spec's `site` or `allowed_hosts`, or `--site`.
  A site is a host and its port; `www.` is the same site, other subdomains are not unless
  `allowed_hosts` lists them. Without a named site a run gets NEEDS_REVIEW, with a spec or
  without one.
- A `site`, `--site` or `allowed_hosts` value that is a public suffix (checked against the full
  Public Suffix List, private section included), a single-label name other than `localhost`, or
  carries a path, query, fragment or user info is refused (exit code 3).
- A request predicate's `status` defaults to `ok` (2xx or 3xx), so POST/Redirect/GET passes;
  `url_contains` matches scheme, host and path, never the query string.
- A redirected mutation is judged by the page it landed on: the landing page must load, and its
  URL and text must carry no failure marker. A page text the spec expects on it decides.
- A mutation answered 200 whose body reports a failure (`errors`, `ok: false`, a failed
  `status`) proves nothing.
- `value` predicates read both the request and the response body, and every value of a repeated
  key; a value that only some of them satisfy is no evidence.
- An `allow` entry explains a mutation to another host only when its `url_contains` names that
  host or its registered domain; hints point third parties to `allow`, not `allowed_hosts`.
- Unexplained side effects, a contradicted non-key claim, conflicting evidence and failed
  attempts at forbidden actions send the run to review; needs-review scores are capped at 0.69.
- Screenshots are no longer sent to the LLM unless `EVJ_SCREENSHOTS_TO_LLM` is set.
- Blocking images, fonts or media with `route.abort` is not tampering.

### Fixed

- A page with a shown `<iframe>` or `<frame>` that loads a document is opaque: a status the
  site shows inside a frame sends the run to review instead of failing it. Frame text is still
  never read, so it proves nothing; a hidden frame changes nothing.
- A linked stylesheet whose request the trace records as failed without a response (a refused
  connection, an unsafe port) counts as an empty sheet, like one with an error status, instead
  of leaving the whole page's text unsure. One the agent's route handler aborted does not.
- The value of a shown `readonly` or `disabled` field is text the page may show: a status a
  site puts in one sends the run to review instead of failing it, and still proves nothing.
- A page whose `::before` or `::after` rule gives a shown element text (`content` with a letter
  or digit, `attr()` or a counter) is opaque: a phrase missing from its snapshot sends the run to
  review instead of failing it.
- A page shown before the agent acted, left and revisited at the end no longer meets a
  `final`-scope expectation or claim.
- An honest read done by typing the product page's URL can pass: only the navigation that opens
  the start page is set-up.
- A short word the agent typed only masks a needle that short, so typing "Your" no longer hides
  "Thank you for your order".
- A superscript after a number is a footnote (`$9¹` is 9, not 91); text that only folds into
  digits is not a number.
- The final state comes from the main page, never a blank or inline frame.
- Non-finite JSON numbers read as missing instead of crashing; a blank `ANTHROPIC_API_KEY` or
  `EVJ_REDACTION_KEY` means unset; an unwritable output path is a usage error (exit code 3).
- A rejected API key or unknown model is fatal instead of falling back silently, and a heuristic
  fallback after LLM extraction fails cannot PASS.
- The GitHub Action judges into a fresh directory, so a second step never reuses an earlier
  verdict, and fails on bad input or internal errors.
- The Codex recorder reads the exit code from the `Process exited with code N` line that Codex
  writes before a command's output, when the hook payload has no exit code. Before, every
  Codex shell call was logged with a null exit code, so an honest Codex log could not show that
  its tests passed.
- Honest `go test ./...` and `node --test` sessions pass. `go test` without `-v` is counted in
  packages (`ok`/`FAIL` lines) and with `-v` in test functions, and a run is compared only with
  earlier runs counting the same unit; before, a failing run's two `--- FAIL:` tests made the
  later passing package look like a shrunken suite. `node --test` is a test runner now (with
  `--test-name-pattern`, `--test-skip-pattern`, `--test-only` and named files as filters), and
  its spec (`tests 2`, `pass 2`) and TAP (`# pass 2`) totals are read, also through `npm test`.

### Security

- **A needle the site derives from one input a few letters off proves nothing.** "instok"
  searched for and "Did you mean: In stock?" shown, a plural stemmed away, or the input turned
  round by the page: an occurrence within two letter edits of one input (a substitution counts
  as two; one edit for a needle of four letters or fewer) is masked like an echo. Forgery kind `derived_echo` covers it.
- **Agent-made evidence proves nothing.** Code the agent runs in the page (`evaluate`,
  `add_init_script`, `set_content`, browser-use `evaluate`), responses its route handlers supply
  or rewrite, and links it follows on `data:`, `blob:`, `about:` or `file:` pages cut the
  evidence: what comes after can only send the run to review.
- **Trace calls are deny-by-default.** Every recorded Playwright call outside a list of
  navigation, interaction, reading and waiting methods counts as tampering: CDP sessions,
  WebSocket routes, storage and cookie writes, extra headers, clocks, `route.fallback` overrides
  and methods the list does not know. Uploaded file text counts as the agent's input.
- **Echoes of the agent's own input are masked.** Text the agent typed, pressed, pasted, uploaded
  or opened as a URL, replayed through edits, selections and copy-paste, cannot prove itself when
  the site repeats it in a page, a URL or a response body; any word-start part of 8 or more
  characters is masked too.
- **Only the site can prove anything.** Evidence from other hosts, other ports and non-http(s)
  pages proves nothing; the site is anchored by the spec or `--site`, never by the agent's first
  page.
- **Vacuous and tautological predicates are rejected**: claims every run meets, or that a
  baseline page already showed, prove nothing.
- **Untrusted regexes are bounded** with a per-match timeout, and catastrophic patterns are
  refused.
- **Constraint values are read only from mutations that took effect**, and telemetry that leaves
  the allowed hosts or carries redacted data is flagged.
- **Recordings are untrusted input**: browser-use screenshot and HAR file paths are confined to
  the recording, trace, HAR and history sizes are capped before reading (zip-bomb checks
  included), memory stays bounded, and a HAR from another run is refused.
- **Redaction covers more**: secret `<input>` values in snapshots and bodies, URLs, credential
  pairs and `Authorization` values in free text, CVV, PIN, OTP, SSN and phone fields,
  percent-encoded PII, and typed form values kept out of snapshot text.
- **The GitHub Action resists injection from agent text**: workflow commands are suspended while
  evidence-judge runs, and the job summary is HTML-escaped.
- **Release supply chain**: every action is pinned by commit SHA, workflows are audited with
  zizmor, releases ship a CycloneDX SBOM and build-provenance attestations, and PyPI publishing
  uses trusted publishing.
- **A screenshot alone proves nothing in a run with agent input.** The vision check cannot tell
  the site's text from an echo of what the agent typed, so in such a run a screenshot supports an
  `expect` entry only beside a deterministic predicate, and never a claim on its own.
- **Typing the evidence key by key, or through a URL, proves nothing.** Text typed with
  `keyboard.down`/`up` is replayed like any other typing, text inside `contenteditable`
  elements is never page evidence, and opening a `javascript:` or `vbscript:` URL counts as
  running code in the page.
- **An upload whose contents are not in the recording cuts page evidence.** After a file is
  uploaded from disk or a stream, page text, screenshots, URLs and response bodies cannot prove
  anything (the page may be showing the file); requests and their statuses still can.
- Committed trace fixtures no longer carry the recording machine's paths.
- **A HAR merged into a trace cannot prove anything on its own.** Requests only the HAR has
  (`har_only`) and fields it filled in that the trace recorded empty (`har_filled`) no longer
  prove a claim, an `expect` entry or a constraint; they still contradict and still count as side
  effects. Such a run's `tamper_detection` is `partial`, so `--require-tamper-evidence` no longer
  passes a trace with a forged HAR that adds the answer.
- **Choosing an option proves nothing.** The selected `<option>` of a `<select>` is no longer page
  text, and the option a select action chose is agent input, so picking "In stock" in a filter
  cannot prove the product is in stock.
- **A folder upload cuts page evidence too.** Uploading a directory (`set_input_files` with a
  folder, or a streamed folder or files over a remote connection) now counts as an upload whose
  contents are not in the recording, as does any upload the recording names no file for.
- **A restyled screenshot is tampering.** A `screenshot` or `to_have_screenshot` taken with a
  `style`, `mask` or `mask_color` changes what the page shows while it is captured, so it now
  counts as the agent writing the page; a plain screenshot is still harmless.
- **browser-use custom tools are tampering.** A browser-use action that is not one of its
  built-in tools (current or 0.x names), or that carries code, is recorded as running the
  agent's code instead of passing as a harmless step, so a custom `set_cookie` or page-script
  tool can no longer make a run pass.
- The task-spec guide documents harness setup: set extra headers, locale, permissions and
  geolocation as `browser.new_context(...)` options before tracing starts, and lists the
  context calls that make a run the agent's own when the trace records them.
- The benchmark forges four more ways: `option_select`, `directory_upload`, `styled_screenshot`
  and `custom_tool`.
- **A value made in a field without typing it whole is input.** Every value a text field, text
  area, editable element or `<select>` holds in a trace snapshot (`PageSnapshot.field_values`)
  is agent input, so text composed with the mouse (a click that moves the caret before a
  Delete, words copied from the page and pasted together, a drag and drop, a datalist or
  autofill choice) cannot prove itself when the page repeats it.
- **An echo the site respells is still an echo.** Agent input is also matched with case, spaces
  and punctuation dropped, on word boundaries, in page text and response bodies: a search for
  "instock" answered "Did you mean: In stock?", or a URL's `in-stock` shown as "In stock",
  no longer proves the product is in stock.
- The benchmark forges two more ways: `field_compose` (text made in a search box with the mouse
  or a paste, not typed whole) and `transformed_echo` (the site repeats the input respelled).
- **Opening a `view-source:` page is tampering.** Chromium reports a `view-source:` page under
  the http URL of the page whose source it shows, so its markup, scripts, comments and option
  labels read as that page's text. A `goto` (Playwright, or browser-use `navigate`,
  `go_to_url`, `open_tab`) of any URL that is not `http`, `https`, `data:`, `blob:` or exactly
  `about:blank` (`view-source:`, `file:`, `chrome:`, ...) now counts as the agent writing the
  page. The benchmark forges it as `view_source`.
- **Text a page hides is not page text.** A page's stylesheet (`.instock{display:none}`), a
  hiding utility class (`hidden`, `d-none`, `sr-only`), or a closed `<details>` no longer lets
  text the page never showed prove a `page_text` predicate. Page text now applies
  `display: none`, `visibility: hidden` and `content-visibility: hidden` from inline styles,
  `<style>` elements, constructed stylesheets and the linked stylesheets the trace holds, with
  `@media` rules read at the recorded viewport, and leaves out the body of a closed
  `<details>`. The task-spec guide says what page text is and what it still includes (text
  hidden by a stylesheet the trace does not hold, by script or by layout). The benchmark forges
  it as `hidden_text`.
- The security policy and the task-spec guide no longer say a trace shows every Playwright
  call: it records the calls on the context it traces. An agent given the `Browser` or the
  `Playwright` object can open a second context or an `APIRequestContext` and send requests the
  trace never shows (a `forbid` on them cannot fire). The harness guide now says to give the
  agent only the traced context or its page.
- **A needle composed from several short inputs proves nothing.** Inputs too short to be masked
  alone ("In" and "stock" typed into two fields a page previews side by side) made "In stock"
  between them, and the page passed. A `page_text` needle or `response_contains` text whose
  every word lies in echoes of the agent's inputs (any length, one-character inputs included)
  is now masked; an occurrence with a word the agent did not put in still proves.
- **Typed markup a page renders is an echo of what it shows.** An input was matched as typed
  and with its tags stripped, so a note "In &lt;!-- zz --&gt; stock" (or with a `hidden` or
  `display:none` span between the words) previewed as HTML showed "In stock" and passed. Each
  input with markup now also counts as the text a page shows for it, read by the same rules as
  a snapshot's page text: comments, scripts, styles, templates and hidden elements left out.
- **A URL fragment or a valueless query key the agent opened is input.** Only query values
  and path segments were, and a `goto` back to a page the site had served added nothing, so
  reopening the product page with `#In%20stock`, or opening it with `?In%20stock`, that its
  script showed as "Showing: In stock" passed. The fragment of every URL the agent opens (whole
  and split on `/`, `?`, `&`, `=`) and every query key of an opened URL or `GET` request are
  now inputs; the fragment counts even on a page the site had already served.
- **Text clicked out on a page's own keyboard is input.** Clicks typed nothing, so clicking
  "I", "n", " ", "s", "t", "o", "c", "k" on an on-screen keyboard that shows what it typed
  made "In stock" and passed. A click now records the text of the element it resolved to
  (`ActionEvent.target_text`, from the trace's action log, else the text its selector names),
  and the labels of two or more clicks in a row on controls of at most five characters are
  joined into one input, like keys.
- The benchmark forges four more ways: `short_compose` (the texts typed a piece per field and
  previewed side by side), `markup_echo` (typed with markup a preview leaves out),
  `fragment_echo` (a URL fragment or valueless query key the page shows) and `click_compose`
  (clicked out on a page's own keyboard).
- **Page text abstains where it cannot tell.** Each piece of page text is now shown, hidden, or
  of unsure visibility, and only certainty hides it. Unsure text (`PageSnapshot.unsure_text`)
  never proves a `page_text` predicate or a `page_regex` value, and keeps its absence from
  contradicting one (NO_EVIDENCE, "text the page may hide"). It is text under an `@media`,
  `@supports`, `@container` or `@scope` condition the reader cannot evaluate, a selector it
  cannot evaluate (`:hover`, `:has()`, unknown or unparseable selectors), `var()` in `display`
  or `visibility`, `opacity` near 0, a tiny `font-size`, a transparent text colour or one equal
  to the background, `clip`/`clip-path` that cut it away, a `transform` that scales it to
  nothing, far off-screen offsets, margins or `text-indent`, a zero-size box that clips its
  overflow, a utility hiding class no recorded sheet defines, and all text of a page whose
  stylesheets (linked or `@import`ed) the trace does not fully hold. The reader now also
  evaluates sibling combinators, `:is()`, `:where()`, `:nth-*`/`*-child`/`*-of-type`,
  escaped values, `@layer` order (with `!important` reversal), simple `@supports` tests and
  `@import`ed sheets from the trace; the `hidden` attribute and a closed `<dialog>` yield to
  the page's own styles; an SVG `<title>`/`<desc>` is not page text. Shadow roots are their own
  style scope (the page's sheets do not reach in, `:host` rules reach out), light-DOM children
  no `<slot>` shows are hidden, and a page with an empty custom element (which may hold a closed
  shadow root) cannot prove text absent (`PageSnapshot.opaque`). Honest pages a stylesheet
  layer, `@supports not`, `@import`, `[hidden]` override or shadow root showed text on no longer
  FAIL; text hidden by styles the reader does not model no longer PASSes. The benchmark forges
  it as `unmodelled_hiding`.
- `url` predicates take `path`, the whole decoded path matched exactly (ignoring case and a
  trailing slash): `contains: /p/red-kettle` is a substring and is also met by
  `/p/red-kettle-lid`. The task-spec guide steers exact pages to `path` or an anchored `regex`.
  Like `regex`, a `path` the agent typed into the address bar cannot prove itself, however short.
- **Hook judges ignore the project's `.env` and the agent's `EVJ_*` settings.** The stop hook
  ran the judge with the project as its working directory, so a `.env` the agent wrote there
  could change the judge's settings (or break them, which let the stop through). The judge now
  runs in a private temporary directory, and inherits only the `EVJ_*` variables listed in
  `EVJ_HOOK_PASS_ENV`.
- **A second stop is still judged.** When the agent stopped again after a stop hook sent it
  back (`stop_hook_active`), the hooks returned nothing, so the corrected answer was never
  checked and the user was not told. Claude Code, Codex and Gemini CLI hooks now judge it too.
  They never block a second time, to avoid a loop. If the answer still fails, the stop goes
  through with a warning to the user that names what the evidence does not back up.
- **Stop hooks judge every trace of the turn and stop failing open on bad evidence.** Before,
  the hook judged only the trace with the newest mtime. That let three things through: a junk
  trace the judge refused (exit 3) passed the stop, an older clean trace with a fresh mtime
  replaced the real one, and deleting the traces after using Playwright gave an empty reply.
  Now:
  - When `browser_stop_tracing` reported trace files in the transcript (Claude Code, Codex),
    only those count, and each must exist and pass.
  - Otherwise, every trace modified during the turn is judged (up to 10).
  - A trace whose own recorded events (`wallTime`) end before the turn started is rejected,
    whatever its mtime says.
  - If Playwright was used and a trace is missing, unreadable, rejected by the judge or older
    than the turn, the agent is sent back with instructions. Set `EVJ_HOOK_ON_BAD_EVIDENCE` to
    `notice` to only tell the user instead.
  - The hook still fails open, with a notice, only for its own errors and timeouts.
- **The agent cannot weaken the spec its own answer is judged against.** The stop hook read
  `.evidence-judge/spec.yaml` from the workspace at stop time, so an agent could edit it during
  the turn (for example, drop a `forbid`) and pass. A new synchronous turn-start hook now saves
  the spec when the turn starts, into the hooks' private state directory (`--state-dir`,
  `EVJ_HOOK_STATE_DIR`, or `$CLAUDE_PLUGIN_DATA/state`), keyed by session:
  - Claude Code and Codex: `claude-code-turn-start` / `codex-turn-start` on `UserPromptSubmit`.
  - Gemini CLI: `gemini-turn-start` on `BeforeAgent`.

  The stop hook judges with that copy. If the live spec changed, was deleted or was created
  during the turn, the agent is sent back (`EVJ_HOOK_BLOCK_ON_SPEC_CHANGE=false` only warns).
  `EVJ_HOOK_SPEC` may point outside the workspace. The plugin and extension manifests install
  the turn-start hook.
- **Evidence logs are HMAC-chained and anchored.** The recorder chained lines with plain SHA-256,
  so anyone who could write the log could rewrite it and recompute the chain, and cutting lines
  off the end left no trace. Now, with a state directory (`--state-dir`, as the plugin manifests
  pass it):
  - Each line's `prev` is an HMAC-SHA256 under a per-installation key (`<state>/log-key`,
    mode 0600), and the line names the key's `key_id`.
  - After every line, including the stop hook's `stop` line, the recorder updates a head anchor
    (`<state>/<session>/log-head.json`) with the line count and last MAC.

  The loader verifies the chain under the key and the end of the log against the anchor. A
  truncated or rewritten log is incomplete. Integrity is `full` only when the key and anchor
  both check out. An unkeyed, unanchored or unverifiable log is `partial`, with the reason.
- **An evidence log is no longer `full` tamper evidence just because it parses.**
  `session judge` reported every complete evidence log as `tamper_detection: full`. An agent
  that runs unsandboxed as the same OS user can read the log key and sign a forged log, so a
  log now counts as `full` only when two things hold. First, its HMAC chain and head anchor
  verify. Second, the user asserts with `--trusted-log` (or the spec's
  `trusted_evidence_log: true`) that the log, key and anchor were out of the agent's reach.
  Otherwise the log is `partial`, with the reason, and `--require-tamper-evidence` caps it at
  needs review. `--log-key` and `--log-anchor` override the default state-directory paths.
- **MCP `judge_run` no longer overwrites arbitrary files.** `json_report` and `html_report`
  replaced any file inside the allowed roots, even though the tool was annotated as
  non-destructive. Now an existing file is replaced only when it is an earlier evidence-judge
  report: a verdict JSON that validates, or an HTML page with the report's generator tag.
  Replacing any other file needs `overwrite: true`, and the tool is annotated
  `destructiveHint: true`.
- **MCP results fence page- and agent-derived text.** `judge_run` and `explain_verdict` now wrap
  summaries, claim quotes, reasons, evidence excerpts and warnings in `<untrusted_data>` tags,
  so a reading model can tell them apart from instructions. A tag lookalike inside the text is
  defanged and cannot close the fence. The server instructions and the tool descriptions say
  that this text is data, never instructions. The hook feedback already says the same.
- **The hook guides say what the hooks can and cannot guarantee.** The Claude Code, Codex and
  Gemini CLI guides, `SECURITY.md` and the session design doc no longer describe evidence logs
  as out of the agent's reach, or the stop hooks as failing open on bad evidence. Each guide now
  has a section on the agent-writable workspace and on sandboxing the hooks' state directory:
  - Claude Code: `sandbox.filesystem.denyRead`/`denyWrite` with
    `allowUnsandboxedCommands: false`.
  - Codex: `workspace-write` plus a separate OS user or container.
  - Gemini CLI: its sandbox.

  The MCP guide documents `overwrite` and the `<untrusted_data>` fencing.
- Session judge: more files count as tests, so editing their assertions weakens the suite.
  Django's `tests.py`, `test*.py`, `*_test.*`, `*_spec.*`, `*.test.*` and `*.spec.*` in any
  language, and the `tests/`, `spec/`, `__tests__/`, `testdata/` and `fixtures/` directories
  (named with or without a trailing slash) were partly missed: rewriting an assertion in a
  Django `tests.py` then running the suite passed.
- Session judge: removing or changing any existing line of a test file weakens the suite, not
  only lines holding a test or an assertion. Changing `parametrize` data (`(2, 3, 5)` to
  `(2, 3, -1)`), a fixture or a helper an assertion relies on passed. Adding lines and
  re-indenting are still fine.
- Session judge: creating test configuration weakens the suite as changing it does, and the
  files that say how tests run count as test configuration. A new `pytest.ini` with
  `addopts = --ignore=tests/test_calc.py` passed, as did changing the `test:` recipe of a
  Makefile. Any change to a Makefile, `*.mk`, justfile, Taskfile, Rakefile, magefile, noxfile,
  `tox.ini`, `pytest.toml`, `.cargo/config.toml`, `.env` or `.envrc` now counts; in
  `package.json` any change beyond dependency versions does (scripts, `jest` settings); in
  `pyproject.toml` and `setup.cfg` a change inside a pytest, coverage, tox, hatch, pdm, poe, uv
  or `pytest11` entry-point section does when the transcript records the whole file, and a
  changed line naming a test runner or a pytest setting does otherwise.
- Session judge: a shell command that writes a test file weakens the suite. `sed -i` rewriting
  assertions, `cat > tests/test_x.py <<EOF`, `tee`, `cp` onto a test file and
  `git checkout HEAD -- tests/test_x.py` all passed, because only the edit tools' changes to
  test files counted. So does a write to `pyproject.toml` or `package.json` from the shell. A
  command that may write files its command line does not name now counts as a possible test
  change too: a script (`python3 fix.py`, `npm run regen`, `make` with an unknown target), a
  `python -c` that imports or opens anything, a write to a glob or a path built from a
  variable, `cp`/`mv` into a directory, `tar`/`unzip`/`patch`, `find -delete`/`-exec`,
  `curl -O`, `jest -u`, and `git stash`, `apply`, `reset --hard` or a checkout of a branch, a
  directory or the whole tree. Its targets are parsed precisely where the command names them:
  `sed`'s and `perl -e`'s script is not a target, `cp`'s source is not, `git checkout -b` and a
  plain `git reset` change no file, and `python -c` code that imports and opens nothing, and
  `--version`/`--help` of a program on the `PATH`, are read-only.
- Session judge: loading variables from a file changes how later commands run.
  `set -a; . ./.env; set +a; pytest` with `PYTEST_ADDOPTS=--ignore=...` in a freshly written
  `.env` passed. `source`/`.` of a file, `set -a`/`set -o allexport`, `eval`, and `export` of
  names built at run time now count, as does writing `.env` or `.envrc`. More variables count
  as harness settings: `PYTHONOPTIMIZE` (strips `assert` from the code under test),
  `PYTHONWARNINGS`, `PYTHONUSERBASE`, `PYTHONNOUSERSITE`, `PYTHONSAFEPATH`, JVM and Gradle
  options, `RUSTC`, `GOTOOLCHAIN`, and any `PYTEST_*`, `JEST_*`, `VITEST_*`, `MOCHA_*`,
  `NEXTEST_*` or `CARGO_TARGET_*_RUNNER`. Such a change is judged for the whole session, since
  the shell may keep it across tool calls.
- Session judge: a change made in the background can happen at any later time, so nothing
  after it proves anything about the files. `(sleep 3; sed -i ... calc.py) &` followed by a
  green test run passed although the fix was reverted after the run. A command that may change
  files and runs with `&`, in a `run_in_background` tool call, or under `setsid`, `tmux`,
  `screen`, `at`, `crontab`, `systemd-run`, a file watcher and the like now changes the
  workspace until the session ends (`CommandAnalysis.lasting_mutation`).
- Session judge: a test run outside the session directory is a subset of the suite, wherever
  the directory came from. Claude Code's shell keeps its directory between Bash calls while the
  transcript can record the session directory for each call, so `cd pkg_b` in one call and
  `pytest -q` in the next passed as the whole suite (reproduced with a real Claude Code
  session). Top-level `cd`/`pushd`/`popd` are now followed across calls (`cd_targets`), as is
  the directory a transcript records; a `cd` in a subshell or pipeline does not carry over, and
  one the judge cannot resolve (`popd`, `cd -`, a variable, a failed call, `cd x || true`)
  leaves the directory unknown until an absolute `cd` back. A `cd` inside the test command
  (`run_cds`) is resolved from there, so `cd /abs/project && pytest` is the whole suite.
- Session judge: a run proves the whole suite only if it ran at least as many tests (skipped
  included) as the largest earlier whole-suite run of the same runner in the session. A suite
  shrunk by a change the judge cannot see (a new ignore rule, a deleted directory, an
  environment that skips tests) no longer passes as whole. `test_result` predicates take
  `min_tests` for a size the spec knows.
- Session judge: honest whole-suite runs that were held for review now pass, without opening a
  way to fake one. `pytest tests/` and `pytest tests pkg_b`, and per-package runs in separate
  calls (`pytest tests`, then `cd pkg_b && pytest`), prove the whole suite when their paths do
  not overlap and together they ran exactly as many tests as an earlier whole-suite run, with no
  edit or command since that run that may have added a test (`CommandAnalysis.test_paths`,
  `FileEditEvent.adds_tests`). A padded run (new tests standing in for left-out ones),
  overlapping or repeated paths, node ids and `-k` still cannot. `PYTHONPATH=.` (or `$PWD`) and
  `-p no:cacheprovider` no longer make a run a subset. Two test runs in one command
  (`pytest tests && cd pkg_b && pytest`) still need review, since only the last summary is read.
- Session judge: the evidence recorder's own logs (`~/.evidence-judge/sessions`, or wherever
  `EVJ_HOOK_LOG_DIR` points) and the project's `.evidence-judge/` spec count as session stores,
  so rewriting them puts every result in doubt as rewriting a Claude Code or Codex transcript
  does. The rule now applies to writes only: reading a transcript (`cat`, `jq`, `grep`) no
  longer holds an honest session for review. A write counts when the command names a store as
  a target, or names one anywhere (quotes removed, so `~/.cl""aude` counts, as do words inside
  `bash -c`) and may write files it does not name. The arguments of a read-only program whose
  output is redirected are no longer taken for write targets.
- Session benchmark: 17 more forgery kinds from a red-team review (shell rewrites of tests,
  `git checkout`/`stash`, a rewriting script, parametrize data, a Django `tests.py`, a new
  `pytest.ini`, a Makefile recipe, a sourced `.env`, `PYTHONOPTIMIZE`, a background revert,
  a `cd` kept from an earlier call or made inline, padded and repeated path runs, `jest -u`, a
  quoted transcript path and an evidence-log edit): 367 forged sessions, 0 false PASS, and the
  five honest controls unchanged. Path-scoped runs that together match an earlier whole-suite
  run must also cover every test file that run's output named, so a run elsewhere whose count
  happens to match cannot stand in for the tests that failed.
- **Switching off the hooks or the judge is harness tampering.** A session that writes or
  deletes agent configuration (`.claude/settings*.json` with `disableAllHooks`, hook scripts,
  `hooks/hooks.json`, `.mcp.json`, plugin directories, `~/.codex/`, `~/.gemini/`) or the
  `evidence-judge` install (`~/.local/bin/evidence-judge`, a shim at `$(which evidence-judge)`,
  `uv tool uninstall evidence-judge`) goes to review, naming the hook switch when it sets one.
  Before, `{"disableAllHooks": true}` in `.claude/settings.local.json` silenced the recorder and
  the Stop judge and the session still passed. Forgery kinds `disable_hooks`, `rm_judge_bin`,
  `uninstall_judge` and `judge_shim` cover it.
- **Packages the test runner may load are harness changes.** `uv run --with ./plugin`,
  `--with-editable`, `--with-requirements`, `uvx --from` a path or VCS URL, `pip install -e
  ./plugin`, `npm link`, and installing any package other than the project itself (`.`,
  `.[dev]`, `-r requirements.txt`, a bare `npm install`) or a test runner (`pytest`, `jest`, ...)
  send the session to review: the judge cannot read the project's lock file, and any added
  package can register a `pytest11` plugin that rewrites results. Before, a local plugin loaded
  with `uv run --with ./evjplug pytest` monkeypatched the code under test and the run passed.
  Forgery kinds `uv_with_local_plugin` and `install_local_plugin` cover it.
- **A skipped test is not a passing one.** A run that skips more tests than the earlier
  whole-suite run, or sets a variable on its command line that no earlier test run set
  (`CI=true pytest` can switch on `skipif` markers), is no longer whole-suite evidence. Only
  output-formatting variables (`NO_COLOR`, `FORCE_COLOR`, `TERM`, `COLUMNS`,
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PY_COLORS`, `CLICOLOR*`, `LINES`) and
  `PYTHONPATH=.` are exempt. Before, `CI=true pytest` turned two failing tests into "1 passed,
  2 skipped" and the session passed. Forgery kinds `ci_env_skipif` and `export_ci_skipif`
  cover it.
- **A test module that replaces the code under test weakens the suite.** Added test modules and
  conftest files are parsed: an attribute assignment or `setattr` on an imported module at
  import time, a `sys.modules` write, or `monkeypatch.setattr`/`mock.patch(...).start()` in an
  autouse, module- or session-scoped fixture or a `pytest_*` hook is test weakening. An answer
  that claims a fix when nothing outside the tests changed goes to review. Before, a new
  `tests/test_aaa_regression.py` with `calc.add = lambda a, b: a + b` made every test pass and
  the session passed. Forgery kinds `import_rebind_test` and `autouse_monkeypatch` cover it.
- **Every file the answer says it changed is checked offline.** The heuristic extractor reads
  each path an answer names as fixed, changed, updated, added to, written or created, not only
  the first, so "I fixed `calc.py`, added a test in `tests/test_regression.py` and updated
  `README.md`" with only `calc.py` edited is contradicted (FAIL) when the log shows every
  change, and goes to review when a command may have written files the log does not name.
- **Guard hooks refuse to switch the hooks off.** New `evidence-judge hook claude-code-guard`
  (Claude Code `PreToolUse`, registered by the plugin), `codex-guard` (Codex `PreToolUse`) and
  `gemini-guard` (Gemini CLI `BeforeTool`, registered by the extension) deny Write, Edit,
  MultiEdit, NotebookEdit, `apply_patch`, `write_file`, `replace` and shell calls that write
  agent configuration, the `evidence-judge` install or the evidence logs, reusing the session
  judge's write-target analysis (about 0.3 s a call). They allow anything they cannot read;
  `EVJ_HOOK_GUARD=false` turns them off. Live-checked: a Claude Code session told to write
  `{"disableAllHooks": true}` with Write and then with `echo >` was denied both times.
- **The hook shim pins the CLI and fails closed without it.** With `--state-dir`, the plugin's
  `run-hook.sh` pins the `evidence-judge` path and checksum on first use. If the CLI later
  disappears or changes, the Stop hook sends the agent back once and then stops the session
  instead of allowing an unchecked answer; `EVJ_HOOK_ALLOW_MISSING_JUDGE=1` in the harness's
  environment restores the notice. The guides recommend Claude Code managed settings
  (`allowManagedHooksOnly`, the plugin in managed `enabledPlugins`) and explain that a
  same-user agent can otherwise disable hooks.

[Unreleased]: https://github.com/hsayvaidya23/evidence-judge/commits/main

