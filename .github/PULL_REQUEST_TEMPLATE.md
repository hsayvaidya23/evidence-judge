## What and why

<!-- What does this change, and what problem does it solve? Link the issue. -->

## How it was verified

<!-- Commands run, fixtures used, before/after verdicts. -->

## Checklist

- [ ] `make ci` passes (ruff, format, pyright strict, unit tests)
- [ ] New behaviour has tests; new checkers cite the evidence they used
- [ ] No unredacted traces, HAR files, screenshots or secrets are committed
- [ ] `schemas/run-verdict.schema.json` regenerated if `RunVerdict` changed
- [ ] Docs updated if CLI flags, task-spec fields or verdict semantics changed
