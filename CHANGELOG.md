# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). What SemVer covers (the CLI, the verdict JSON and
the names exported from `evidence_judge`) is defined under
[Stability](docs/reference/python-api.md#stability).

## [Unreleased]

The first release. Nothing was released before it, so this entry lists what the tool does;
fixes made during development are in the [security history](docs/security-history.md).

### Added

**Browser judge**

- `evidence-judge judge` grades a browser agent's final answer against what the run recorded:
  Playwright traces (format v7–v10), HAR 1.2 and browser-use histories, merged into one
  normalised run. Outcomes are PASS, FAIL or NEEDS_REVIEW with CI exit codes (`0`, `1`, `2`;
  `3` bad input, `4` internal), scored on six dimensions: success, side effects, state,
  guardrails, recovery and loops.
- Claims are extracted with Claude (verbatim-quote grounding, prompt-injection defences) or
  offline by heuristics, and checked by deterministic predicates (`request`, `page_text`,
  `url`, `action`, `value`, `screenshot`). Evidence counts only from the named site, text that
  echoes the agent's own input proves nothing, and text the page may hide is unsure.
- Text under a full-screen opaque box reads as unsure, also when the box is out of sight now
  but a fade-in or another animation, or a `:hover` or `:target` rule, may show it.
  A preloader that fades out and stays out (one animation that ends within five seconds
  filling forwards) covers nothing.
- A claimed price is matched in an order's JSON answer only as its one total, on the order's
  own path (the root, `order`, `checkout`, `payment`, `invoice`, `data`, ...), never in a
  sub-object, a line item, a non-payment Stripe object or a body that reports a failure at
  any depth (which also leaves the order unproven), and only in the currency every field names.
  Amounts read as minor units only in Stripe-shaped objects, never because a currency code is
  lowercase (Medusa). Plain decimal string totals (Shopify, WooCommerce,
  PayPal) count.
- Task specs (`expect`, `forbid`, `constraints`, `allow`, `site`, `allowed_hosts`,
  `failure_markers`, `trusted_scripts`) are validated on load, with errors naming line and
  column. A PASS needs a named site. Verdicts report `tamper_detection`, and
  `--require-tamper-evidence` caps the rest at NEEDS_REVIEW.
- Reports: versioned JSON (`evidence-judge/run-verdict@1`), a self-contained HTML page
  (WCAG AA, works on phones) and the console; `inspect` summarises evidence without judging.

**Coding-agent session judge**

- `evidence-judge session judge` grades a Claude Code transcript, a Codex `exec --json` stream
  or rollout, or an evidence-recorder log against its tool-call log. Claims such as "all tests
  pass", "I fixed the bug in `x.py`" or "I only changed `calc.py`" compile to `test_result`,
  `command`, `edit`, `build`, lint and type-check predicates. A passing test run proves a pass
  only when it ran the whole suite to the end after the last change, with no test-weakening
  edit or command. `session inspect` lists what the judge sees; `session judge --html` writes
  the HTML report, `--task` sets the task, and `--otel` / `--langfuse-trace-id` export the
  verdict as `judge` does.
- A session spec's `trusted_wrappers` (`{"make test": "pytest -q"}`) declares what a project
  wrapper runs; the declaration stops applying once the session may have changed it.
- A silent wrapper's count floor survives writes of data files (`.gitignore`,
  `config/app.yaml`, test fixtures) unless a Makefile the session showed may read them; Makefile
  fragments, wrapper configuration and files of unknown kind still void it.
- Spec and claim regexes run under the browser checks' 50 ms budget; a search that runs past
  it needs review. Path globs match in linear time, support `[...]` and `[!...]` classes, and
  reject `{a,b}` and backslash escapes; a path outside the session directory matches no glob, so
  `only` fails an edit outside the project and `ignore_edits` never ignores one.

**Independent re-run**

- A session spec's `rerun:` block lets `session judge --rerun` copy the working tree, run the
  suite there without a shell in an environment built from scratch, and decide test results
  from the runner's own report (pytest JUnit XML, `go test -json`, jest/vitest JSON, node's
  JUnit reporter, libtest JSON, unittest). Verdicts carry `verification` (`rerun` or
  `transcript`); `--require-rerun` refuses a pass without one; `session rerun` judges a re-run
  without a session record; `rerun.include` adds gitignored paths and `rerun.sandbox` runs the
  command under an OS sandbox.

**Benchmarks**

- `evidence-judge bench`: false-PASS rates with Wilson intervals on forged browser runs,
  accuracy on honest controls and an optional transcript LLM-judge baseline. The forgery lab
  covers the four manipulation kinds from "Gaming the Judge" plus evidence attacks on the
  recording, on local demo sites (a shop, a clinic booking form, an admin console).
- Every variant of every forgery kind runs on every failed run that can carry it (one seed per
  variant when a kind has more variants than seeds), with a per-variant table in
  `bench/results`.
- A session forgery benchmark (`python -m evidence_judge.sessions.bench`) over recorded
  red-team and honest sessions from Claude Code and Codex, with Python, Node and Go projects.
  Current results are in [the benchmark page](docs/benchmark.md) and `bench/results`.

**Integrations**

- CLI: `judge`, `inspect`, `bench`, `schema`, `version`, `mcp`, `hook ...` and
  `session judge|rerun|inspect`; `--env-file` names a dotenv file. Piping the output keeps the
  verdict's exit code.
- MCP server (`evidence-judge mcp`, the `mcp` extra): `judge_run`, `judge_session`,
  `inspect_run`, `explain_verdict`, `validate_spec` and `find_recordings`, over stdio or
  streamable HTTP on `127.0.0.1`, confined to its `--root` directories; it never runs a re-run.
- Claude Code plugin and marketplace: a Stop hook that sends the agent back on FAIL, recorder
  hooks keeping an HMAC-chained evidence log, a turn-start hook that saves the specs, a guard
  hook that refuses writes switching the hooks off, the MCP server and a skill.
- Codex CLI and Gemini CLI: the same hooks (`evidence-judge hook ...`) and a Gemini CLI
  extension.
- GitHub Action for browser runs (with a `site` input) and sessions (`session-transcript`,
  `session-format`, `session-spec`), with a job summary, strict boolean inputs and an optional HTML report
  artifact; a Docker image.
- Python API: `Judge`, `SessionJudge`, `load_run`, `load_session`, `TaskSpec`, `SessionSpec`,
  `RunVerdict`, `Redactor`, `build_llm` and `read_settings`, exported from `evidence_judge`.
- LLM backends: the Anthropic API or a logged-in Claude Code, Codex or Gemini CLI (`--llm`,
  `EVJ_LLM_BACKEND`), run headless with tools, MCP servers and hooks off; a cost budget per
  run, a concurrency cap and a disk cache.
- OpenTelemetry `gen_ai.evaluation.result` events, Langfuse scores, Arize Phoenix span
  annotations and an Inspect AI scorer.

**Redaction and security model**

- Secrets and PII are redacted from evidence, answers, specs and reports by default (CLI, MCP
  server, Action and Python API alike), with HMAC fingerprints so redacted values still match
  and a stable per-installation key so LLM cache hits survive restarts. A fingerprint is never
  read as text the agent typed, so a verdict does not depend on the key.
- Credentials on command lines (`mysql -pX`, `curl -u user:X`, `sshpass -p`, `docker login -p`,
  `htpasswd -b`, `npm`/`aws` `config set`, `--token X`) are redacted by tool and flag, while
  paths, file names and test ids after a secret's name stay readable.
- Command lines continued with a backslash, or cut off inside a quote, are redacted too, and
  command redaction runs in linear time (a line of 5,000 `--token` flags takes a fraction of a
  second, not minutes). A test id, path or small count after a known tool's secret flag
  (`curl --token tests/test_x.py::test_a`, `npm test --password 2 failed`) is kept.
- Screenshots reach the LLM only when `EVJ_SCREENSHOTS_TO_LLM` is set.

**Docs**

- A documentation site: getting started for browser runs and Claude Code sessions, concepts,
  a spec cookbook, guides for every integration, the CLI, configuration, verdict schema and
  Python API references, the benchmark, the security policy and the security history.

### Changed

For anyone upgrading from a pre-release snapshot:

- **Breaking:** no `.env` file is read implicitly any more. Name a dotenv file with the global
  `--env-file PATH` option or `EVJ_ENV_FILE=PATH`; real environment variables still win over
  it. Use `read_settings()` from Python, since `Settings()` reads environment variables only.
- `Judge`, `SessionJudge` and `BenchmarkRunner` redact their inputs when `EVJ_REDACT` is on;
  `JudgeOptions(redact=False)` and `SessionJudgeOptions(redact=False)` mark inputs that are
  already redacted. `SessionJudge` takes `Settings` first, like `Judge`.
- `judge --llm anthropic`, `bench --online` and `bench --with-llm-baseline` without
  `ANTHROPIC_API_KEY` exit `4` instead of judging offline or exiting `3`. The GitHub Action
  rejects boolean inputs other than `true` or `false`.

### Security

The threat model, what the judge can and cannot detect, and how to report a vulnerability are
in [SECURITY.md](SECURITY.md). The judge was red-teamed in repeated rounds before this release;
every attack that worked became a recorded fixture or benchmark forgery kind and was closed.
[docs/security-history.md](docs/security-history.md) keeps that record, including the
pre-release fixes once listed here.

[Unreleased]: https://github.com/hsayvaidya23/evidence-judge/commits/main
