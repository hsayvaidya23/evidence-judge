# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Ingest of Playwright traces (format v7–v10), HAR 1.2 and browser-use history into one normalised `Run` model.
- Claim extraction with Claude (structured output, verbatim-quote validation, prompt-injection defences) and an offline heuristic extractor.
- Typed predicates (`request`, `page_text`, `url`, `action`, `value`, `screenshot`) with deterministic checkers; the screenshot vision check runs only as a last resort.
- Task specs with `expect`, `forbid`, `constraints`, `allow` and `allowed_hosts`.
- Six verdict dimensions: success, side effects, state, guardrails, recovery and loops.
- PASS / FAIL / NEEDS_REVIEW outcomes; evidence absence counts only on captured channels.
- Secret and PII redaction with HMAC fingerprints so redacted values still match.
- JSON (versioned schema), self-contained HTML and console reports.
- `evidence-judge` CLI (`judge`, `inspect`, `schema`, `version`) with CI-friendly exit codes.
- LLM budget, disk cache and fake client for tests.
- Demo shop and seven recorded agent runs with a golden regression test.
- Forgery lab: deterministic and LLM forgers for the four manipulation kinds from "Gaming the Judge" plus a style-only control.
- `evidence-judge bench`: false-PASS rates with Wilson intervals for Evidence Judge (with and without a spec) and a transcript LLM-judge baseline.
- Integrations: OpenTelemetry `gen_ai.evaluation.result` events, Langfuse scores, Arize Phoenix span annotations and an Inspect AI scorer (optional extras).
- Docker image (non-root, multi-stage) built and smoke-tested in CI.

### Security
- Confined browser-use screenshot paths, zip-bomb and size limits for traces, HAR mismatch detection, redaction of secret `<input>` values in snapshots.
