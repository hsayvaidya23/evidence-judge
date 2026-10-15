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

- Every verdict says how much tampering its source could reveal (`tamper_detection`: `full`
  for a Playwright trace, `partial` for a browser-use history, `none` for a HAR alone) and warns
  when it is not `full`; `--require-tamper-evidence` (or the spec's `require_tamper_evidence`)
  caps such runs at `NEEDS_REVIEW`.

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

### Security

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
- **Opening a `view-source:` page is tampering.** Chromium reports a `view-source:` page under
  the http URL of the page whose source it shows, so its markup, scripts, comments and option
  labels read as that page's text. A `goto` (Playwright, or browser-use `navigate`,
  `go_to_url`, `open_tab`) of any URL that is not `http`, `https`, `data:`, `blob:` or exactly
  `about:blank` (`view-source:`, `file:`, `chrome:`, ...) now counts as the agent writing the
  page. The benchmark forges it as `view_source`.

[Unreleased]: https://github.com/hsayvaidya23/evidence-judge/commits/main

