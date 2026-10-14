# Architecture

Evidence Judge answers one question about an agent run: **is what the agent says supported by
what the browser recorded?** It does not read the agent's answer and decide whether it sounds
right. It turns the answer into checkable claims and checks each one against evidence.

## Pipeline

```
  trace.zip ─┐
  run.har  ──┼─▶ ingest ──▶ redact ──▶ extract claims ──▶ compile predicates
  browser-use┘    (Run)     (Run')     (ClaimSet)          (Predicate per claim)
                                                                  │
            ┌─────────────────────────────────────────────────────┘
            ▼
  deterministic checkers ──▶ screenshot fallback ──▶ expectations ──▶ dimensions ──▶ outcome
  (request, url, page_text,  (vision yes/no, only    (TaskSpec       (success,       (pass / fail /
   action, value)             when rules abstain)     expect/forbid/  side effects,   needs_review
                                                      constraints)    state, ...)     + score)
                                                                                         │
                                                                                         ▼
                                                                   reports: JSON · HTML · console
```

| Stage | Module | Input → output |
| --- | --- | --- |
| Ingest | `ingest/` | Playwright trace, HAR, browser-use history → `Run` with ids (`act#`, `net#`, `snap#`, `shot#`, `con#`) and `captured` channels |
| Redact | `redact.py` | `Run` → `Run` with secrets and PII replaced by HMAC-fingerprinted tokens |
| Extract claims | `extract/` | Agent answer and reasoning → `ClaimSet` of atomic claims, each with a verbatim quote. LLM-based, or heuristic with `--offline` |
| Compile predicates | `extract/` | Each claim → one or more `Predicate`s (request, page_text, url, action, value, screenshot) that would prove it |
| Deterministic checkers | `checks/` | Predicate + `EvidenceIndex` → `PredicateResult` (supported / contradicted / no_evidence + citations) |
| Screenshot fallback | `checks/` | Only for claims the rules could not decide: a yes/no question to a vision model on the nearest screenshot |
| Expectations | `engine.py` | `TaskSpec` expect / forbid / constraints evaluated independently of what the agent said |
| Dimensions | `engine.py` | Success, side effects, state, guardrails, recovery, loops, each scored or `None` when not assessable |
| Outcome | `engine.py` | Pass / fail / needs review, a 0..1 score and a one-paragraph summary |
| Reports | `report/` | `RunVerdict` → JSON (schema `evidence-judge/run-verdict@1`), self-contained HTML, rich console |

The CLI (`cli.py`) wires the stages together and maps the outcome to an exit code:
`0` pass, `1` fail, `2` needs review, `3` bad input, `4` internal or LLM error.

## Design rules

1. **Rules before LLMs.** Anything that can be decided from a request, a status code, a URL or
   page text is decided by code. LLMs extract claims and propose predicates; they do not grade.
   The screenshot check is the last resort, and `deterministic_share` in the verdict reports how
   much of the verdict was reached without a model.
2. **Every verdict cites evidence.** A claim is never `supported` or `contradicted` without
   citations to specific events (`net#12`, `snap#4`, `final`). Reports link each citation to
   the event in the timeline.
3. **Absence counts only on captured channels.** "The agent never submitted the form" is a
   contradiction only if the network channel was recorded. If it was not, the result is
   `no_evidence` and expectations report `met: null`.
4. **Agent text is untrusted.** The answer and reasoning are data, never instructions: they are
   delimited in prompts, escaped in HTML, and rendered as plain text in the terminal. An agent
   cannot talk its way into a pass.
5. **Abstain over guessing.** Low-confidence or uncheckable key claims lead to `needs_review`,
   not to a confident wrong verdict. Low claim coverage (below
   `EVJ_REVIEW_COVERAGE_THRESHOLD`) also triggers review. Snapshots are point-in-time, so text
   missing from every snapshot (`scope: any`) contradicts a claim only with confidence 0.6; a
   key claim refuted below `CONFIDENT_CONTRADICTION` (0.7, `checks/outcome.py`) fails the run
   only when another claim is also contradicted or a spec check fails, and otherwise needs
   review. The final page state is authoritative and refutes with full confidence.
6. **Redact before anything leaves the process.** Redaction runs before extraction, before any
   LLM call and before any report is written, and it is deterministic per key so equal values
   still match across claims and evidence.

## Key types

- `Run` (`core/evidence.py`): the normalised recording. `captured` lists the channels present.
- `EvidenceIndex` (`core/index.py`): the only way checkers read evidence.
- `Claim` / `ClaimSet` (`core/claims.py`): what the agent asserted, with verbatim quotes.
- `Predicate` (`core/predicates.py`): a discriminated union of checkable conditions.
- `TaskSpec` (`core/taskspec.py`): what a successful run must contain. See [task-specs.md](task-specs.md).
- `RunVerdict` (`core/verdict.py`): the output. JSON Schema in `schemas/run-verdict.schema.json`.
