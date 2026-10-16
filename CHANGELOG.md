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
  recorded demo-site runs, 0 of 1293 attacks pass.
- A session forgery benchmark (`python -m evidence_judge.sessions.bench`) over recorded
  red-team and honest sessions from Claude Code and Codex.
- Both benchmarks go beyond one shop and one pytest project: a clinic booking form and an
  admin console (`tests/fixtures/sites`, `bench` takes several run directories) and Node and
  Go toy projects with runner-specific tricks; 0 of 1293 browser and 0 of 1377 session
  forgeries pass, and 4 of 38 honest browser controls go to review offline.

**Redaction**

- Secrets and PII are redacted from evidence, answers, specs and reports by default, with HMAC
  fingerprints so redacted values still match, and a stable per-installation key so LLM cache
  hits survive restarts.

**CLI and API**

- `judge`, `inspect`, `bench`, `schema`, `version` (also `--version`), `mcp`, `hook ...` and
  `session judge|rerun|inspect`. Piping the output (`| head -1`) keeps the verdict's exit code.
- A public Python API exported from `evidence_judge`, and a documentation site.

### Fixed

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

### Security

- Hardened over repeated red-team rounds before release: each round's working attacks became
  recorded fixtures and benchmark forgery kinds, and the judge was changed until none of them
  passed. [docs/security-history.md](docs/security-history.md) has the full record, and
  [SECURITY.md](SECURITY.md) the threat model and its limits.

[Unreleased]: https://github.com/hsayvaidya23/evidence-judge/commits/main
