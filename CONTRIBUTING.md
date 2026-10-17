# Contributing

Thanks for helping make agent evaluation more honest. This guide covers the dev setup, the
conventions the codebase relies on, and how to extend the judge with new checkers and evidence
adapters.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or 3.13.

```bash
git clone https://github.com/hsayvaidya23/evidence-judge
cd evidence-judge
make install          # uv sync --locked + Playwright chromium
uv run pre-commit install   # optional: ruff and file checks on every commit
make ci               # ruff, format check, pyright strict, unit tests
make e2e              # end-to-end tests against a real browser
```

LLM-backed checks need `ANTHROPIC_API_KEY` (copy `.env.example` to `.env`). Everything else,
including the whole unit suite, runs offline. Tests marked `live` call a real API and are skipped
unless you run `uv run pytest -m live`.

## Conventions

- **Contracts are stable.** `core/` (evidence, predicates, claims, task spec, verdict, index) is
  the shared contract between ingest, checkers, the engine and reports. Changing it is a design
  decision: open an issue first, and regenerate `schemas/run-verdict.schema.json` with
  `make schema` when `RunVerdict` changes.
- **Types are strict.** `pyright` runs in strict mode over `src/`. Avoid `Any` at module
  boundaries; when a third-party API is untyped, narrow it at the edge.
- **Rules before LLMs.** If a fact can be established from a request, a URL or page text, it
  must be checked deterministically. An LLM may propose a predicate; only code (or, for
  screenshots, a vision check) evaluates it.
- **Every verdict cites evidence.** A checker that returns `supported` or `contradicted` must
  attach citations (`net#12`, `act#3`, `snap#4`, `shot#7`, `con#2`, `final`).
- **Absence only counts on captured channels.** "No such request" is evidence only when the
  network channel is in `Run.captured`; otherwise return `no_evidence`.
- **Agent text is untrusted.** Never interpolate agent output into prompts without delimiting
  it, never render it as markup (HTML is autoescaped; rich output uses `Text`, not markup).
- **Abstain over guessing.** Low confidence becomes `no_evidence` / `needs_review`, not a guess.
- **No secrets in the repo.** Fixtures must be synthetic or redacted. See [SECURITY.md](SECURITY.md).
- Library code never prints; only `cli.py` writes to the terminal.
- Conventional commit messages: `feat(checks): ...`, `fix(ingest): ...`, `test(report): ...`,
  `docs: ...`, `ci: ...`. Keep commits small and focused.
- **Changes to `src/` need a changelog entry** under `[Unreleased]` in `CHANGELOG.md`, written
  for users. CI fails a pull request that changes `src/` without one; a change users cannot
  notice (a refactor, a typo) can carry the `skip-changelog` label instead (then re-run the
  check).

## Adding a checker

A checker evaluates one predicate kind against the evidence index
(`evidence_judge.checks.base.PredicateChecker`):

```python
class UrlChecker:
    kind = "url"
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult: ...
```

1. If you need a new predicate kind, add a model to `core/predicates.py` and include it in the
   `Predicate` union (this is a contract change; see above).
2. Implement the checker in `src/evidence_judge/checks/`, reading evidence only through
   `ctx.index` (`EvidenceIndex`). Deterministic checkers must not touch `ctx.llm`.
3. Return `PredicateResult` with a short, factual `reason`, citations for every piece of
   evidence used, and `status=no_evidence` when the relevant channel was not captured.
4. Register it with the checker registry and add unit tests covering supported, contradicted,
   and not-captured cases, plus at least one adversarial case (the agent claims success, the
   evidence disagrees).

## Adding an evidence adapter

Adapters live in `src/evidence_judge/ingest/` and turn a recording format into a `Run`.

1. Normalise every event into the `core/evidence.py` models with stable, prefixed ids
   (`act#N`, `net#N`, `snap#N`, `shot#N`, `con#N`) and timestamps in milliseconds since run start.
2. Set `Run.captured` to exactly the channels the source actually recorded. Do not claim a
   channel you only partially parsed.
3. Store binary payloads (screenshots) in `Run.blobs` and reference them by key.
4. Truncate bodies to `max_body_chars`; never load unbounded data into memory.
5. Wire the adapter into `load_run` and add a CLI flag if it is a new source type.
6. Add a small synthetic fixture and tests. Never commit real recordings.

## Reports

Report code is in `src/evidence_judge/report/`. The HTML report is a single self-contained file
with a strict Content-Security-Policy: no scripts, no external requests. Keep it that way.

## Releasing

The `evidence-judge` project on PyPI publishes only from this repository's `release.yml`
through trusted publishing; no PyPI token exists. `tests/unit/test_release_notes.py` keeps the
docs honest about it: until `CHANGELOG.md` has a dated release section, every install command
must use `git+https://github.com/hsayvaidya23/evidence-judge`, and once it has one, no
"not released yet" note may remain.

A maintainer cuts releases from `main`:

1. Set `version` in `pyproject.toml` to the release (`0.1.0`, without the `.dev0` suffix) and
   run `uv lock`. The Claude Code plugin's `plugin.json` and the Gemini CLI extension's
   `gemini-extension.json` (under `plugins/`) carry the release version without the suffix;
   when the next release changes, change them with it. `tests/unit/test_plugin_manifests.py`
   fails while they differ from `pyproject.toml`.
2. In `CHANGELOG.md`, rename `[Unreleased]` to a dated section (`## [0.1.0] - 2026-11-02`),
   add a new empty `[Unreleased]` above it, and update the link references at the bottom.
   `release.yml` takes the release notes from the `## [X.Y.Z]` section and fails when there is
   none, so a tag pushed while the notes are still under `[Unreleased]` does not release.
3. Update every pre-release note. They all say "not released yet"; list them with
   `grep -rn "not released yet" README.md SECURITY.md docs src/evidence_judge/_extras.py`.
   Today they are: the install note and the action pinning note in `README.md`, the supported
   versions in `SECURITY.md`, the notes in `docs/index.md` (install and action example),
   `docs/getting-started/install.md`, `docs/cli.md`, `docs/integrations.md`,
   `docs/guides/mcp.md`, `docs/guides/github-action.md` and `docs/faq.md`, and the
   missing-extra hint in `src/evidence_judge/_extras.py`. Point them, and the `git+https://`
   install commands, at PyPI and the tag.
   `tests/unit/test_release_notes.py` fails while any remains and the version has no `.dev`
   suffix.
4. Merge that pull request, then tag the merge commit `vX.Y.Z` and push the tag
   (`git tag -s v0.1.0 -m v0.1.0`, `git push origin v0.1.0`).
5. `release.yml` runs the full CI, checks that the tag matches the package version, builds the
   wheel and sdist with a CycloneDX SBOM, attests their build provenance, publishes to PyPI
   with trusted publishing, and creates the GitHub Release from the changelog section with the
   distributions and SBOM attached.
6. On `main`, set `version` to the next development version (`0.1.1.dev0`).
