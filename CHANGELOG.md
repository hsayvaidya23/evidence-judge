# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). What SemVer covers (the CLI, the verdict JSON and
the names exported from `evidence_judge`) is defined under
[Stability](docs/reference/python-api.md#stability).

## [Unreleased]

The first release.

### Added

**Browser-agent judge**

- `evidence-judge judge` grades a browser agent's final answer against what the run recorded:
  Playwright traces (format v7–v10), HAR 1.2 and browser-use histories, merged into one
  normalised run. Outcomes are PASS, FAIL or NEEDS_REVIEW with CI exit codes (`0`, `1`, `2`;
  `3` bad input, `4` internal), scored on six dimensions: success, side effects, state,
  guardrails, recovery and loops.
- Claims are extracted with Claude (verbatim-quote grounding, prompt-injection defences) or
  offline by heuristics, and checked by deterministic predicates (`request`, `page_text`,
  `url`, `action`, `value`, `screenshot`); the screenshot vision check runs only as a last
  resort. Evidence counts only from the named site, and text that echoes the agent's own input
  proves nothing.
- Task specs with `expect`, `forbid`, `constraints`, `allow`, `site`, `allowed_hosts`,
  `failure_markers` and `trusted_scripts`, validated when they load; errors name their line and
  column. A PASS needs a named site: the spec's `site` or `allowed_hosts`, or `--site`.
- Every verdict says how much tampering its source could reveal (`tamper_detection`), and
  `--require-tamper-evidence` caps the rest at NEEDS_REVIEW.
- Reports: JSON (versioned schema `evidence-judge/run-verdict@1`), a self-contained HTML page
  that meets WCAG AA contrast and works on phones, and the console. `inspect` summarises
  evidence without judging it.

**Coding-agent session judge**

- `evidence-judge session judge` grades a Claude Code transcript, a Codex `exec --json` stream
  or rollout, or an evidence-recorder log against its tool-call log. Claims such as "all tests
  pass", "I fixed the bug in `x.py`" or "I didn't touch the tests" compile to `test_result`,
  `command`, `edit` and `build` predicates; session specs add their own. A passing test run
  proves a pass only when it ran the whole suite after the last change, with no test-weakening
  edit or command in the session. `session inspect` lists what the judge sees.
- Independent re-run: a session spec's `rerun:` block lets `session judge --rerun` copy the
  working tree, run the suite there without a shell in an environment built from scratch, and
  decide test results from the runner's own report (pytest JUnit XML, `go test -json`,
  jest/vitest JSON, node's JUnit reporter, libtest JSON, unittest). Verdicts carry
  `verification` (`rerun` or `transcript`); `--require-rerun` refuses a pass without a re-run;
  `session rerun` judges a re-run without a session record; `rerun.include` adds gitignored
  paths to the copy and `rerun.sandbox` runs the command under an OS sandbox.
- A session spec's `trusted_wrappers` (`{"make test": "pytest -q"}`) declares what a project
  wrapper runs, so an honest run through a wrapper the session does not show (`make test`
  echoing `pytest -q` with no Makefile shown) can pass. The declaration stops applying once the
  session may have changed what the wrapper runs (an edit or shell write to anything but source
  code or prose, a harness change, a `cd`, a subagent or an MCP tool), and a definition the
  session showed outranks it.

**Integrations**

- Claude Code plugin and marketplace: a Stop hook that sends the agent back on FAIL, recorder
  hooks that keep an HMAC-chained evidence log, a turn-start hook that saves the specs before
  the agent acts, a guard hook that refuses writes switching the hooks off, the MCP server and
  a skill.
- Codex CLI and Gemini CLI: the same stop, record, turn-start and guard hooks
  (`evidence-judge hook ...`), and a Gemini CLI extension.
- MCP server (`evidence-judge mcp`, the `mcp` extra) with `judge_run`, `inspect_run`,
  `explain_verdict`, `validate_spec` and `find_recordings` tools, over stdio or streamable HTTP
  on `127.0.0.1`, reading and writing only inside its `--root` directories.
- LLM backends: the Anthropic API, or the Claude Code, Codex or Gemini CLI you are logged into
  (`--llm`, `EVJ_LLM_BACKEND`), run headless with tools, MCP servers and hooks off; a cost
  budget per run, a concurrency cap and a disk cache.
- OpenTelemetry `gen_ai.evaluation.result` events, Langfuse scores, Arize Phoenix span
  annotations and an Inspect AI scorer; a reusable GitHub Action; a Docker image.

**Benchmark**

- `evidence-judge bench`: false-PASS rates with Wilson intervals on forged runs, accuracy on
  honest controls, and an optional transcript LLM-judge baseline. The forgery lab covers the
  four manipulation kinds from "Gaming the Judge" and evidence attacks on the recording; on the
  recorded demo-site runs, 0 of 1374 attacks pass.
- A session forgery benchmark (`python -m evidence_judge.sessions.bench`) over recorded
  red-team and honest sessions from Claude Code and Codex.
- Both benchmarks go beyond one shop and one pytest project: a clinic booking form and an
  admin console (`tests/fixtures/sites`, `bench` takes several run directories) and Node and
  Go toy projects with runner-specific tricks; 0 of 1446 browser and 0 of 1653 session
  forgeries pass, and 4 of 43 honest browser controls go to review offline.
- Forgery kind `negated_lead_in` words a forged success after a negation that does not reach
  the claims ("Not wanting to overpay, compared prices and bought ...", "I didn't see any errors
  and got order #..."), so a claim reader that carries the negation over shows up in `make
  bench` as the no-spec judge's FAIL turning into NEEDS_REVIEW (15 of 33 to 1 of 33).

**Redaction**

- Secrets and PII are redacted from evidence, answers, specs and reports by default, with HMAC
  fingerprints so redacted values still match, and a stable per-installation key so LLM cache
  hits survive restarts.

**CLI and API**

- `judge`, `inspect`, `bench`, `schema`, `version` (also `--version`), `mcp`, `hook ...` and
  `session judge|rerun|inspect`. Piping the output (`| head -1`) keeps the verdict's exit code.
- A public Python API exported from `evidence_judge`, and a documentation site.
- The session judge is part of the public Python API: `SessionJudge`, `SessionJudgeOptions`,
  `SessionSpec` and `load_session` are exported from `evidence_judge` and documented beside
  `Judge`.
- `session judge --html PATH` writes the same self-contained HTML report as `judge --html`, for
  a session: claims with the tool calls they cite (command and exit code), spec expectations,
  the verification method (re-run or transcript), warnings, and every recorded tool call as the
  timeline. It shows the redacted session and escapes all agent text.
- `session judge --llm BACKEND` picks the backend for session claim extraction, as `judge --llm`
  does; a chosen backend that cannot be used (such as `anthropic` without `ANTHROPIC_API_KEY`)
  exits `4` instead of judging offline.

### Changed

- The package description and keywords cover coding-agent sessions as well as browser agents,
  `session --help` names Gemini CLI (judged through the evidence log), and the Claude Code
  plugin's `Stop` hook says it is checking the answer against the recorded evidence instead of
  "the browser trace", since it also re-runs the session's tests.
- The Claude Code plugin and marketplace descriptions, the Gemini CLI extension, the Claude
  Code, Codex CLI and Gemini CLI guides' descriptions, the README, the docs home page and the
  site description mention coding-agent sessions and the independent test re-run, not only
  browser traces.
- **Breaking:** no `.env` file is read implicitly any more. Name a dotenv file with the global
  `--env-file PATH` option or `EVJ_ENV_FILE=PATH`; real environment variables still win over
  it. Anyone who kept settings in a `.env` next to where they run the CLI must pass it
  explicitly.
- Every install command in the docs (the MCP client snippets for Claude Code, Codex CLI and
  Gemini CLI, and the `pip install` lines in the CLI and integrations pages) installs from the
  GitHub repository until v0.1.0 is on PyPI, so none of them runs whatever is published under
  the unclaimed PyPI name; a test keeps it that way.
- The README and docs home page benchmark numbers match `bench/results` again (44 attack kinds,
  1398 runs per judge, 37 evidence-tampering kinds, 37/41 honest controls), and a test fails
  when they drift. The architecture page covers the session judge, hooks and MCP server, and a
  new getting-started page walks through judging a Claude Code session.
- `pre-commit` is a dev dependency, so the `pre-commit install` step in CONTRIBUTING works after
  `make install`; the configuration reference lists every `EVJ_HOOK_*` variable; the verdict
  schema example imports `RunVerdict` from `evidence_judge`; the install page says the
  integrations read their own credentials; the sdist ships `Makefile` and `uv.lock`, and the
  package declares the operating systems it is tested on (Linux and macOS).

### Fixed

- `judge --llm anthropic` without `ANTHROPIC_API_KEY` now exits `4` and names the missing key,
  like the CLI backends do when their CLI is missing, instead of judging offline and exiting
  `0`. Without `--llm`, a missing key still judges offline.
- An evidence log with no valid record now says "no valid evidence-log records were found"
  (with the first line's problem, such as "no version field") instead of "unsupported version
  None"; it is still bad input (exit 3).
- The offline session judge no longer FAILs honest reports worded "No tests failed.", "None of
  the 3 tests failed.", "0/3 tests failed." or "1/3 tests pass.": zero, slash and worded counts
  are read in the right direction, and a pattern-read claim that tests fail, contradicted by a
  passing run, now needs review instead of failing the run.
- Offline claim extraction no longer reads honest negated wordings the wrong way round:
  "Nothing was purchased", "no message was sent", "Nothing in her profile was changed" used to
  become completion claims that FAILED the run, and "Not all tests pass" a passing-suite claim.
  Both extractors now abstain on any clause holding a negation word they have no tested rule
  for; "not a single test failed", "not all tests pass" and "2 fail" are read in the right
  direction, and amounts after "limit", "under", "shipping" or "fee" are not price claims.
- The offline session judge no longer PASSes a claimed fix on a session whose tests still fail
  when the fix shares its sentence with an unrelated negation ("I fixed the bug in `calc.py`,
  nothing else changed.", "... and didn't break anything.", "... without touching the
  tests."): only a negated clause about tests or outcomes cancels the passing suite a fix
  implies, and "I didn't touch X" no longer hides the rest of its sentence. Independently of
  extraction, an asserted fix in an answer that reports no test result is checked against the
  final test run or re-run, and a failing re-run the answer does not report needs review. A
  leading negated noun ("No problems, placed the order") or a finite verb after a negated
  auxiliary ("I didn't hit any errors, and ordered it") no longer hides the claims after it;
  questions, "pass for X, not for Y" and "Ran 3 tests in 0.01s: OK." are read as they mean.
- The session judge no longer PASSes a run whose final test run or re-run fails because the fix
  was worded outside the patterns ("The issue has been fixed in `calc.py`.", "Bug fixed in
  `calc.py`.", "I patched the bug", "Corrected the sign error", "I updated `calc.py` so `add`
  returns the sum.") or the answer understated the failures ("1 unrelated test still fails"
  against three, "`test_network` still fails" when other tests fail, "`test_add` passes" when it
  fails). On red evidence it now denies a pass by default: only a supported claim that tests
  fail, whose count matches the run's and whose named failing tests are among the run's, lets
  the run pass; anything else needs review. Claims that tests fail carry the count and test
  names the answer gives, and both are checked against the run or re-run; reported failures the
  evidence does not show, and code changed after the last deciding run with no supported test
  claim, need review too. Passive, participle and repair wordings of a fix are recognised by one
  shared definition. Honest answers that report a failure in another sentence than the fix
  ("I fixed `test_add`. `test_add_negative` still fails.") or count both sides ("2 tests pass
  and 1 fails.") are no longer FAILed.
- With the LLM claim extractor, a red session no longer PASSes when the extracted claim that
  tests fail drops the count or test name the answer states ("1 test still fails" against two);
  the answer's own count and names are checked against the run, and a mismatch needs review.
- The session judge no longer PASSes "`test_sub` passes; `test_add` still fails." or "1 test
  still fails" on a failing run that stopped early (`pytest -x`, `--maxfail`, `jest --bail`,
  `go test -failfast`, `cargo test` without `--no-fail-fast`, pytest's `stopping after N
  failures` banner) or was filtered (`-k`, a node id, `--lf`): such a run shows neither how many
  tests fail nor that a test it does not list passed. Counts of failing tests and "not among its
  failures" now need a whole-suite run that ran to the end; otherwise the run needs review.
- The session judge no longer PASSes a count of failing tests against a run in which a whole
  unit of tests failed to load: a Go package that did not build (`FAIL pkg [build failed]`) or
  panicked, a jest or vitest suite that failed to run, a pytest module that failed to collect,
  or a unittest module that failed to import. Their tests are in no count, so such a run shows
  how many tests fail no more than it shows which; the count is unknown and the run needs
  review.
- An honest "1 test still fails: `test_a`." no longer FAILs under the LLM extractor when the
  run shows `1 failed, 1 error` for a test that failed and then errored in teardown, or two
  failing parameters of one test: a count of failing tests now matches the summary's count or
  the distinct tests the output names, with or without parameters. A claim that tests fail
  whose count is still wrong now needs review instead of failing the run, whatever the
  extractor; it never passes.
- A claimed fix beside a vague report of failures ("Some tests still fail.", "A few unrelated
  tests are still failing.") no longer PASSes a red session, nor does a report naming a runner
  that did not fail ("The jest tests fail." on a pytest run): with a claimed fix, a claim that
  tests fail must count or name them, or the tests failing at the end must be a strict subset
  of those failing before the first change, and a runner it names must be the one that failed.
  Anything else needs review.
- The session benchmark's Markdown report keeps an honest control whose answer holds a newline
  or a pipe on one table row: both are escaped in the cell.
- Offline claim extraction no longer hides a forged browser answer's claims behind a leading
  negated participle ("Not wanting to overpay, compared prices and bought the Red Kettle") or
  an irregular past verb after a negated auxiliary ("I didn't see errors and bought it"): with
  no spec these reached only NEEDS_REVIEW and now FAIL. "I never logged in, added it to the
  cart or placed an order" and "I didn't add it to the cart or place an order" still abstain.
- The session judge no longer PASSes "All tests pass." on a pytest run whose output reports a
  strict XPASS (`XPASS(strict) tests/x.py::test_c`, `[XPASS(strict)]`) while its summary counts
  the test as `xpassed`: pytest counts a strict XPASS as a failure, so the run is red with that
  test among its failures. Forgery kind `trick:strict_xpass` covers it.
- Honest session answers that report a clean linter or type checker ("All tests pass, and ruff
  passes with no issues.", "…mypy found no errors.", "No lint errors.") no longer go to review
  as reports of failing tests: they are lint and type-check claims checked against the runs,
  and a negated clause whose subject is a linter or type checker and that names no tests is
  not a test-failure report. Three honest controls cover them.
- "I only changed `calc.py`.", "No other files were modified." and "no other files changed" are
  now checked: `edit` predicates take an `only` list of globs that every recorded change must
  match, the offline extractor reads these wordings (with the files the answer names as
  changed), and the LLM prompt asks for them. A session that also edited `utils.py` used to
  PASS and now FAILs; "I fixed `calc.py` and didn't touch any other files" no longer reads as
  "no file changed", which FAILed the honest fix. Forgery kind `false_only` covers it.
- Lint and type-check claims ("ruff check passes", "mypy reports no errors") no longer rest on
  the exit status alone. A run whose own output counts errors fails whatever its exit status
  (`ruff check --exit-zero .` printing `Found 3 errors.`), `--exit-zero`, pylint
  `--fail-under` and `--issues-exit-code=0` mask the status, and a passing run over explicit
  files, a rule or file selection (`--select`, `--ignore`, `--exclude`, pylint `--disable`,
  mypy `--disable-error-code`), another configuration, `--follow-imports=skip` or another
  directory (`cd /tmp/x && ruff check .`) cannot prove the claim. A newly added `# noqa`,
  `# type: ignore`, `eslint-disable`, `@ts-ignore` or similar comment is treated like a skip
  marker for that category. Each of these used to PASS; forgery kinds `check_claim:*` cover
  them.
- The session judge no longer PASSes answers whose claims it cannot check. "Committed and pushed
  to origin/main.", "I committed the change" and "Installed `requests`" become `git commit`,
  `git push` and install command claims that must have succeeded: with no such command in a
  complete log, or a failing one, the run FAILs. Any other action only a command could show
  ("Deployed to staging", "opened a PR") and coverage or speed figures ("100% coverage", "10x
  faster") that no claim covers send the run to review, as does a claim the LLM extractor
  quoted from the answer but could not keep whole (no valid predicate, a predicate that did not
  validate, over the claim cap). A command claim now defaults to `ok: true` unless the command
  is a test, build, lint, type or format check; specs keep `ok` unset to mean "it ran". Session
  verdicts now report `claim_coverage`. Forgery kinds `unchecked_claim:*` cover it.
- A Gemini shell call whose output prints `Exit Code: 0` no longer records as a success: the
  recorder and the log reader take the exit code from the last `Exit Code:` line, the trailer
  Gemini writes after the output, and only from `llmContent`. Before, a failing `ruff check .`
  preceded by `printf 'Exit Code: 0\n'` was logged with exit code 0, and "ruff check passes."
  PASSed. The Codex recorder reads `Process exited with code N` only from the header Codex
  writes before the command's `Output:`.
- Session ingest no longer reads a single huge line whole before applying the 64 MiB line cap:
  lines are read at most that far, the rest of a longer line is skipped in 1 MiB chunks, and
  the line is reported as oversized, as before.
- A session file given under the wrong option (`session judge --codex` with a Claude Code
  transcript) or an empty one exits `3` (bad input), naming the format the file looks like.
  Before, nothing was recognised, only a warning about skipped records was printed, and the run
  got NEEDS_REVIEW (exit `2`).
- Evidence-log problem messages name the file line, not the record number: a record missing
  `cwd` on line 3 of a log whose first line is not JSON is reported as line 3, not line 1. The
  same holds for a broken `seq`/`prev` chain.
- "Installed `requests`." after a successful `pip install -r requirements.txt` no longer FAILs:
  when no install command names the package but a later install from a requirements, manifest
  or lock file succeeded (`pip install -r`, `pip install -e .`, `uv sync`, `uv pip sync`,
  `poetry install`, `pipenv install`, `npm ci`, a bare `npm install`, `yarn`, `pnpm install`,
  `bundle install`, `go mod download`, `cargo fetch`), the claim has no evidence and the run
  goes to review. With no install at all, or only a failed one, it still FAILs. A new honest
  session control covers it.
- "The build still fails." (also "the build is still failing", "it still fails to compile") is
  read as a claim that the build fails. With no build command after the last change, the last
  test run failing to compile (Go's `FAIL pkg [build failed]`, cargo's `error: could not
  compile`) supports it, and it accounts for that failing run when the run names no failing
  test, so an honest report of a broken Go or Rust build PASSes instead of going to review. A
  passing build sends the claim to review ("check the wording"), not to FAIL. Multi-target cargo
  output (`test result: FAILED. 1 passed; 1 failed`, a `failures:` section) is covered by
  end-to-end tests: counts and test names were already read. A Go session control covers the
  build failure.
- The page-text reader's selector matching no longer backtracks exponentially on deep pages:
  50 nested `div`s under `section div div div div div{display:none}` took 6.3 s and 100 never
  finished. Each compound's answer for an element, and whether some ancestor or earlier sibling
  matches, is worked out once and kept on the element; a selector whose ancestor compounds no
  ancestor has is rejected at once; a match that still takes more than 50,000 tests counts as
  "may match", so the element is unsure, not a hang.
- The page-text reader no longer counts a value it cannot read as "does not hide". `opacity:
  var(--o)` with `--o: 0`, `opacity: calc(0)`, `transform: scale(calc(0))`, `color: var(--c)`
  with `--c: transparent`, `font-size: max(0px, 0px)` and `left: calc(-10000px)` on an
  absolutely positioned element passed a stock check whose "Out of stock" text they hid; they
  now need review, like `opacity: 0`. Custom properties are now cascaded and substituted into
  `var()` (fallbacks, cycles, `@property` initial values and inheritance, `:hover`-style rules
  that may set them), `env()` safe-area insets read as 0, `calc()`, `min()`, `max()`,
  `clamp()` and `abs()` are evaluated, nested function calls are read whole, and colours cover
  every CSS named and system colour, `hwb()`, `lab()`, `lch()`, `oklab()`, `oklch()`,
  `color()`, `color-mix()` and `light-dark()`. What is still unreadable in a property that can
  hide text (an unknown function, unit or keyword, `attr()`, a custom property it cannot
  tell) makes the text unsure. A `background-image` now hides the background colour from the
  colour comparison, and of two inline declarations of one property the later one wins (the
  reader kept the first).
- The page-text reader now treats hiding it did not model as unsure instead of shown. These
  hid the "Out of stock" text of a stock check that then passed: `mask-image` (and
  `-webkit-mask-image`), `filter: blur(60px)`, `transform: rotate3d(0,1,0,90deg)`, `rotate: y
  90deg`, `skewX(90deg)`, `clip-path: path(...)` or `rect(...)`, `height: 0` with `contain:
  paint` or `overflow: auto`, `color: #fefefe` or `snow` on white, `position: fixed; top:
  -100vh`, `inset-inline-start: -10000px`, `text-indent: 100%` with `overflow: hidden`, and
  the SVG `<text>` attributes `font-size="0"`, `transform="scale(0)"`, `clip-path` and
  `fill="white"`. Any mask or motion path is unsure; a filter is unless slight or
  luminance-keeping; `transform`, `rotate` and `scale` are composed into one matrix and are
  unsure when it squashes, stretches or turns the box edge-on, or holds a function the reader
  does not know; clip paths are measured on a line of text and on the viewport; text whose
  colour (or text fill, or SVG `fill`, with its own and its ancestors' opacity) has a WCAG
  contrast below 1.2 with the background is unsure, and every CSS named colour is known;
  logical insets and margins, a fixed box outside the viewport and positive image-replacement
  indents count as off-screen; SVG presentation attributes are read like the properties they
  name. `unmodelled_hiding` forgeries now render their page through the reader with 15
  variants covering these, so a reader regression becomes a false PASS in the benchmark. A
  full-page opaque overlay painted over the text is still not modelled (it needs layout).
- The page-text reader unescapes property names as a browser does. `--\63: transparent` with
  `color: var(--c)`, `--\64: none` with `display: var(--d)`, and `co\6cor: transparent` hid
  text the reader showed: it unescaped values but not names, so `--\63` never matched `--c`.
  A declaration now splits at its first unescaped colon (`--a\:b` is one name), escapes in
  `@property` names and descriptors are read, and `!\69mportant` counts as `!important`. New
  `unmodelled_hiding` variant `escaped_custom_property`; it was a false SHOWN before the fix.
- A `background-image` no longer switches off the colour comparison. `color: #fff` over
  `linear-gradient(transparent, transparent)` or `url(data:,)` read as shown (it was unsure
  before the colour comparison learnt about images). The reader now works out the colours a
  gradient paints (each stop over what is behind it, `-webkit-gradient()` too) and finds text
  unsure when it is faint against any of them; an empty image (`url()`, a `data:` URL with no
  data), a fully transparent gradient and a layer of zero `background-size` paint nothing; a
  gradient whose colours it cannot tell (relative colour syntax, too many colours) is unsure.
  An image it cannot see (`url()` with content, `image-set()`) is still taken to give the text
  contrast, as documented. New `unmodelled_hiding` variants `transparent_gradient`,
  `empty_image` and `zero_size_image`; each was a false SHOWN before the fix.
- Adversarial CSS no longer crashes the judge. `color: lab(1e308 1e308 1e308)` raised
  OverflowError, and a `var()` chain 400 deep, `calc()` nested 3000 deep, `color-mix()` nested
  500 deep, deep `var()` fallbacks, `:is()` / `:not()`, `@supports` conditions and nested blocks
  raised RecursionError out of the snapshot reader. Nesting is now capped at 32 levels (past it
  the value is unknown, so the text is unsure, and a stylesheet nested deeper leaves its scope
  unknown), unary signs are read without recursion, colour channels are clamped, and numbers
  too large to compute with are unreadable. As a last defence, a snapshot whose styles still
  make the reader raise is read without them: all of its text unsure and the page opaque,
  never shown. Seeded fuzz tests cover the value parser and colour functions.
- A test run through a project wrapper (`make test`, `npm test`, `yarn test`, `pnpm test`,
  `tox`, `nox`, `just test`, `poetry run task test`) is no longer taken as a whole run to the
  end when the session does not show what the wrapper ran: its pass or fail still counts, but
  it backs no count of failing tests and no "`test_x` is not among the failures", so a stop at
  the first failure or a filter inside the wrapper cannot hide failures. The judge reads the
  wrapper's command from its own output (npm's `> jest` header, tox's `commands[0]>` line,
  nox's `nox >` line, make's recipe echo) or from a `Makefile` or `package.json` the session
  showed whole, and judges it like a direct run; lint and type-check wrappers (`make lint`,
  `npm run lint`) are read the same way, and make's `Error 1 (ignored)` or a `-` recipe line
  masks the exit status. jest's `Test Suites: 1 failed, 1 of 3 total` counts as stopping early.
  An honest failure count reported through a silent `make test` with no Makefile shown now
  needs review.
- A green run through a wrapper whose command the session does not show (a silent make recipe
  with no Makefile shown, a yarn 2+ script) is no longer taken as the whole suite: "all tests
  pass" on it needs review, unless an earlier run of the same framework in the session, whole,
  run to the end and with its command shown, ran no more tests than the wrapper run passed
  (plus any the session added since). The judge now also reads a `justfile` and a `tox.ini`
  the session showed whole (plain recipes; a test environment's `commands` and every
  `setenv`/`set_env`), the variables a Makefile exports (`export PYTEST_ADDOPTS = -x`), and a
  `SKIP` exported by an earlier command before `pre-commit run`.
- A wrapper's echo counts only when the wrapper printed it: not with `make -s`, `npm
  --silent`, `--loglevel`, `pnpm --reporter`, `yarn -s`, `tox -q` or `tox -p`, not when npm's or
  pnpm's `> pkg@1.0.0 script` banner (yarn 1's `yarn run v1` banner, bun's `$` line) does not
  open the output or names the script twice, and make's recipe echo only beside a Makefile the
  session showed (one the judge cannot read whole) with no recipe line that can run silently
  (`@`, `$(Q)`, `.SILENT`), since an `@echo pytest; pytest -k x` recipe prints the same echo. A
  lint or type-check run through a wrapper whose command stays unseen no longer decides a clean
  claim by its exit status. An honest `make test` or `make lint` with no Makefile shown now
  needs review.
- A Makefile the session showed is no longer read when it can change what its recipe runs: a
  function call anywhere (`$(shell ...)` runs while make reads the file), an assignment to
  `SHELL`, `MAKEFLAGS` or another make variable, a special target other than `.PHONY`,
  `.DELETE_ON_ERROR`, `.PRECIOUS`, `.SECONDARY`, `.INTERMEDIATE`, `.NOTPARALLEL` and `.SUFFIXES`
  (`.IGNORE`, `.SILENT`, `.EXPORT_ALL_VARIABLES`, `.ONESHELL`), or a variable in a target or
  prerequisite. `:=` assignments expand where they stand. A recipe line before the test or
  check that may change files (a prerequisite's `rm tests/...` or `echo ... > conftest.py`)
  leaves the wrapper unseen and is checked like a command of its own, so a deleted test file or
  a written `conftest.py` is tampering.
- A wrapper's echo no longer counts beside an `.npmrc` the session showed with a `loglevel`,
  `silent`, `quiet` or `reporter` setting (npm then prints no header, so the script can print
  one). A tox `setenv`/`set_env` now counts from a `setup.cfg`'s `[testenv]` sections and a
  `pyproject.toml`'s `[tool.tox]` (its `legacy_tox_ini` or native `set_env` tables) as well as a
  `tox.ini`, and one the judge cannot read (a `file|` line) leaves tox's echo unread. A silent
  wrapper's green run is no longer measured against an earlier whole-suite count when the
  session changed the tests since in a way the judge cannot count (a test written by a command,
  a parametrize case, a removed or deleted test). 30 forged sessions passed before; none does now.
- A lint or type-check claim no longer passes after a suppression comment written from the
  shell (`sed -i 's/$/  # noqa/'`, `perl -pi`, a heredoc or `echo` appended to a source file,
  `python -c` rewriting one), or on a `pre-commit run` without `--all-files`, with a hook id,
  `--files`, a ref range or `SKIP=...`. "Pushed to origin/main" (or "to `origin/fix`", "to the
  main branch") is checked against the remote and branch a successful `git push` names or
  git's ref lines show: a push elsewhere FAILs, and a dry run or one that shows neither needs
  review.
- Page text with a soft hyphen or a zero-width character inside a word (`Out of st<U+00AD>ock`,
  as CMS hyphenation writes it) no longer FAILs an honest run: text normalisation drops every
  default-ignorable code point (soft hyphen, zero-width space, joiners, word joiner, BOM,
  bidi and variation marks, tag characters) before matching, in page text, response bodies and
  claims alike.
- An order or reference number claimed from inside a longer one no longer passes: "The shop
  confirmed order #100" (or `#001`) was supported by `/orders/1001`. A `page_text`,
  `body_contains` or `response_contains` phrase holding a digit now matches only a whole token:
  no digit beside a digit at either end, no letter beside a letter or digit, and no decimal or
  group separator joining it to more digits (`$24` still matches `$24.00`, not `$24.50`). Other
  phrases are still substrings. New forgery kind `prefix_reference` (write) places the order and
  claims a number that is a prefix, suffix or middle part of the one the shop gave it; it
  produced false PASSes before the fix and none after.
- Offline extraction reads what a purchase cost when no price word names it: "I bought the Red
  Kettle for $4.00", "placed at $4.00" and "the kettle ($4.00)" become price claims, so the
  honest purchase answered with the wrong price no longer PASSes with coverage 1.0 (it goes to
  review; the true $24.00 still PASSes). Amounts after "limit", "under", "shipping" or "fee" are
  still not price claims, nor is one with extras added ("for $26.40 including tax"), nor a
  difference ("$6 under the budget"); "under $30 for $4.00" bounds only the $30. Claim coverage
  also counts a claim-like sentence stating a currency amount that no claim's predicates check
  as not covered. New forgery kind `misquoted_price` (write) places the order, gets the claimed
  number, is charged another price and claims "for $4.00"; it produced false PASSes before the
  fix and none after. The benchmark now has 44 kinds: 0 of 1374 attack runs pass, honest
  controls unchanged.
- A price the agent says it paid is checked against what the site answered the purchase with,
  not against every page of the run: "bought it for $29.00" used to PASS when another item was
  listed at $29.00 and the order was charged $29.50. Offline extraction now proves an amount in
  a sentence that asserts a purchase, or in an answer that does ("It was $29.00"), with a new
  `request` criterion `result_contains`: the response body, or the page the request led to
  before the agent acted again (a page the agent opened itself never counts; a payment
  provider's off-site challenge is passed through). A recorded answer that leaves the amount
  out contradicts it only weakly (review); none recorded is no evidence. A value stated with a
  copula and no price word in the answer ("It was $4.00", "the balance is $1,284.50") is now a
  claim and counts toward claim coverage. New forgery kind `listed_price` (write) opens a page
  listing another item at $29.00, is charged $29.50, $29.99 or $28.00 and claims "for $29.00";
  it produced 6 false PASSes before the fix and none after. The benchmark now has 45 kinds: 0
  of 1398 attack runs pass, honest controls unchanged (34, 27 and 2 of 38 correct). Matching
  of caseless scripts (Chinese, Japanese) at a letter end stays a substring match; documented.
- Whole-token matching of numbers and IDs no longer finds `100` in `1 100`, `1 100` (narrow
  no-break space) or `1'100`: a space or apostrophe between digit groups joins them. An ID of
  three or more digits is not found in `1001A`, `A1001` or `1001-A` (a short run still is:
  `10:00` in `10:00am`), and NFKC no longer folds `2⁴` into `24` (superscript and subscript
  digits normalise to `^4` and `_4`). Forgery kind `prefix_reference` gains two variants, a
  letter (`#4316A`) and a dash and a letter (`#4316-B`) after the claimed number: 5 false
  PASSes over five seeds before the fix, none after. Bench unchanged: 0 of 1398 attacks pass,
  honest controls 34, 27 and 2 of 38.
- An honest price in a locale format no longer goes to review: a phrase that is only an amount
  of money (`$24`, `EUR 1.249,50`) is compared by currency and amount, so "bought it for $24"
  is met by `USD 24.00`, `$ 24`, `$\xa024`, `US$24` or `24 $`, and `€1.249,50` by
  `€1,249.50`, never by another currency or amount. Offline extraction reads `€1.249,50`
  whole (it read `€1.249`), a decimal comma (`$24,00`) and a currency after the amount
  (`24 USD`, `1.249,50 €`). New honest control `spaced_price` writes every dollar amount of a
  passing run as `$ 24.00`: before the fix its three runs went to review or failed with every
  spec, after it they pass. Bench: 0 of 1398 attacks pass; honest controls 37, 30 and 2 of 41
  correct (34, 27 and 2 of 38 plus the three new ones with each spec).
- A price given only in a reasoning step no longer passes on a page that lists it for another
  item: with "Total: $29.00" in the reasoning and "Bought it." as the answer, the total was
  checked as `page_text` anywhere and passed while the order was charged $29.50. In a run whose
  answer or reasoning asserts a purchase, every price or stated amount (answer or reasoning) is
  now checked against what the site answered the order with. An honest reasoning step that
  prices another item in such a run ("The Blue Kettle costs $29.00") now goes to review. New
  forgery kind `reasoning_price` (write) plants the `listed_price` evidence with the price only
  in the reasoning ("Total: $29.00.", "It came to $29.00.", "The price was $29.00."): 12 false
  PASSes before the fix, none after.
- An order answered in JSON with the price as a bare number now shows it: `result_contains:
  ["$29.50"]` is met by `{"total": 29.5}`, `{"grandTotal": 29.5}` or `{"amount_cents": 2950}`
  (minor units only when the key says so), in the currency of the nearest `currency` field
  (`{"total": 29.5, "currency": "EUR"}` does not show `$29.50`). Numbers under any other key
  (`order_id`, `qty`, `item_count`) show no price. New honest control `json_confirmation`
  answers the order with `{"total": 24.0, "total_cents": 2400, "currency": "USD", "qty": 1}`
  and strips the price from the pages after it: before the fix its two runs went to review with
  every spec, after it they pass. New forgery kind `json_total` (write) answers the order with
  another total beside a quantity and a line id equal to the claimed amount, the claimed amount
  in euros, or in cents; none passed before or after.
- One space between two digit groups in a money amount (`$24 100`) is no longer read as 24100:
  it may be $24 followed by 100, so it meets neither `$24` nor `$24,100` (as text it still meets
  `$24 100`). A second group or a decimal part settles it (`$1 024 100`, `24 100,00 €`).
- With Claude, a claim about what a purchase cost keeps only predicates that check the order's
  answer (a `POST`, `PUT` or `PATCH` to a named URL with the amount in `result_contains` or
  `response_contains`); a `page_text`, `value` or other predicate on the amount is replaced by
  the offline extractor's order predicate, so an amount listed on any page no longer proves it.
  Bench: 0 of 1446 attacks pass (47 kinds); honest controls 39, 32 and 2 of 43 correct (37, 30
  and 2 of 41 plus the two new ones with each spec).
- The GitHub Action checks its boolean inputs: `offline`, `fail-on-review` and the new
  `upload-report` take `true` or `false` in any case, and any other value (`yes`, `1`) fails
  the step before anything is installed; before, everything but the exact string `true` was
  silently read as `false`. New inputs `anthropic-api-key` (masked, set only for the judging
  step, and an empty one keeps a job-level `ANTHROPIC_API_KEY`) and `upload-report` /
  `report-artifact` (upload the HTML report as an artifact, also on FAIL); `offline: false`
  without a key now warns that the run is judged offline. The API key requirement is
  documented in the action guide.
- The wall-clock budgets of the large-trace and large-stylesheet tests are tighter (3 s and
  12 s, from 10 s and 20 s; they take about 0.5 s and 5 s).
- "Committed and pushed to origin/main" no longer PASSes on an echoed ref line. A `git push`
  counts only when the line's exit status is its own (only `&&` after it, no pipe, `!` or `&`)
  and its output has no `! [rejected]`, `error: failed to push` or `fatal:`; ref lines are read
  only after git's last `To <url>` line on a line where the push runs last; and a refspec naming
  another branch contradicts the claim whatever the output shows. `git push origin main; echo
  '... main -> main'` after a rejected push, the same after `git push origin fix`, and a
  `git push origin fix` whose hook prints a `main` ref line PASSed before. New forgery kinds
  `push_target:echo_after_rejected` and `push_target:echo_other_branch` (0 false PASS; 1503
  session forgeries); `git push origin main || true` now needs review.
- A background that only a rule the reader cannot evaluate sets (a `:hover`, `:focus` or
  `:checked` rule, one under an unknown `@media`) no longer switches off the colour comparison:
  `.panel:hover{background:#000}` with white text on a white page read as shown. Every
  background such a rule may paint now counts, and the text is unsure when it is faint against
  any; rules for the same state of the same element (a `:hover` rule, or Tailwind's
  `hover:bg-*` and `hover:text-*`, that swaps the colour and the background) are compared with
  each other only, so honest buttons stay shown. Text over a background picture (`url()`,
  `image-set()`) the reader cannot see is unsure when it is near white or near black and no
  solid `background-color` under the picture contrasts with it; a gradient scrim painted over
  the picture counts. On ten saved real-site pages 0.1% more text reads as unsure (Tailwind's
  docs 5.4% to 7.0%, mostly white captions over `from-transparent` gradients; the others
  unchanged or within 0.1%). New `unmodelled_hiding` variants `state_background` and
  `image_background`; each was a false SHOWN before the fix.
- The page-text reader now sees four more ways to hide text it showed. An opaque box painted
  over it: a fixed or absolute box with an opaque background colour covering the viewport or
  its containing block (`inset: 0`, or `top`/`left` 0 with 100% or `100vw`/`100vh` sizes)
  makes the text under it unsure, unless the text is inside it or a positioned ancestor lifts
  it higher (a larger `z-index`, or the same one later in the page); one with a negative
  `z-index`, a translucent one and a hidden one cover nothing. `backface-visibility: hidden`
  on a box it or an ancestor turns to show its back (`rotateY(180deg)`). An `animation` whose
  `@keyframes` may remove, hide or obscure the text at any point (`to{opacity:0}` with
  `forwards`, a fade-in from `opacity: 0` too), or one it cannot name. A 3D transform under an
  ancestor's `perspective` (`translateZ(-100000px)`), unless it stays in the page's plane.
  Transitions need nothing new: the reader already judges the value they end on. On ten saved
  real-site pages 0.17% more text reads as unsure, all of it from fade-in animations (Stripe
  15.6% to 16.8%, Vercel 9.0% to 9.5%). New `unmodelled_hiding` variants `opaque_overlay`,
  `backface_flip`, `ending_animation` and `perspective_depth`; each was a false SHOWN before.

### Security

- Hardened over repeated red-team rounds before release: each round's working attacks became
  recorded fixtures and benchmark forgery kinds, and the judge was changed until none of them
  passed. [docs/security-history.md](docs/security-history.md) has the full record, and
  [SECURITY.md](SECURITY.md) the threat model and its limits.
- A `.env` in the working directory can no longer change settings. The GitHub Action runs in
  `$GITHUB_WORKSPACE`, so a pull request's `.env` could set `EVJ_REDACT=false` (an unredacted
  report uploaded as an artifact) or `EVJ_REVIEW_COVERAGE_THRESHOLD=0` (a needs-review run
  passing); the CLI and MCP server read it the same way. The Action also warns when its
  workflow sets `EVJ_REDACT=false`.
- The Python API now redacts: `Judge.judge` redacts the run, spec and claims before claim
  extraction and any LLM call when `EVJ_REDACT` is on, as SECURITY.md says. The CLI, MCP server
  and Inspect scorer go through the same `Judge.redact`, so redaction happens once and the same
  way everywhere; `JudgeOptions(redact=False)` marks inputs that are already redacted.
- `SessionJudge` redacts too: `SessionJudge.judge` redacts the session (commands, outputs,
  task, answer), spec and claims before claim extraction when `EVJ_REDACT` is on, so a
  transcript judged from Python no longer reaches an LLM extractor with its secrets. `session
  judge` goes through the same `SessionJudge.redact`; `SessionJudgeOptions(redact=False)` marks
  inputs that are already redacted, and `SessionJudge` now takes `Settings` first, like `Judge`.

[Unreleased]: https://github.com/hsayvaidya23/evidence-judge/commits/main
