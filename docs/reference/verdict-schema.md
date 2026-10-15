---
description: The RunVerdict JSON format (evidence-judge/run-verdict@1) - fields, types and the full JSON Schema.
---

# Verdict JSON schema

`evidence-judge judge --out verdict.json` writes a `RunVerdict` as JSON. Its `schema_id` is
`evidence-judge/run-verdict@1`; the version changes only when the format changes incompatibly.

Print the JSON Schema with the CLI, or use the copy in the repository at
`schemas/run-verdict.schema.json`:

```bash
evidence-judge schema                        # to stdout
evidence-judge schema --out run-verdict.schema.json
```

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_id` | string | `evidence-judge/run-verdict@1` |
| `judge_version` | string | The evidence-judge version that produced it |
| `run_id` | string | The run's id (`--run-id` overrides it) |
| `task` | string \| null | The task text |
| `outcome` | `pass` \| `fail` \| `needs_review` | The decision |
| `score` | number 0..1 | Weighted score; at most 0.49 for a FAIL |
| `summary` | string | One paragraph naming the deciding rule and evidence |
| `claims` | `ClaimVerdict[]` | Each claim: `claim_id`, `claim_text`, `quote`, `kind`, `key`, `status`, `reason`, `citations`, `checker`, `confidence` |
| `expectations` | `ExpectationResult[]` | Each spec check: `expectation_id`, `kind` (`expect` \| `forbid` \| `constraint`), `met` (true \| false \| null), `reason`, `citations` |
| `dimensions` | object | `success`, `side_effects`, `state`, `guardrails`, `recovery`, `loops` → `{score: number \| null, findings: string[]}` |
| `claim_coverage` | number \| null | Share of claim-like sentences that became claims |
| `deterministic_share` | number \| null | Share of claim verdicts decided without an LLM |
| `tamper_detection` | object \| null | `{level, reason}`: what the evidence source can show of the agent tampering with the page or its requests. `level` is `full` (Playwright trace), `partial` (browser-use history) or `none` (HAR only); null only in verdicts that did not assess it |
| `verification` | object \| null | `{method, reason}` for a coding-agent session: `rerun` when the judge ran the suite itself and read the runner's report (`session judge --rerun`), `transcript` when test results come from the output the session recorded; null for browser runs |
| `warnings` | string[] | E.g. missing channels, dropped hallucinated quotes, extraction fallback |
| `usage` | object | `llm_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms` |

A citation is `{evidence_id, excerpt}`, with ids like `net#12`, `act#3`, `snap#4`, `shot#7`,
`con#2` or `final`.

## Using it

=== "jq"

    ```bash
    jq -r '.outcome, .summary' verdict.json
    jq -r '.claims[] | select(.key and .status == "contradicted") | .claim_text' verdict.json
    ```

=== "Python"

    ```python
    from pathlib import Path
    from evidence_judge.core.verdict import RunVerdict

    verdict = RunVerdict.model_validate_json(Path("verdict.json").read_bytes())
    failed = [c for c in verdict.claims if c.key and c.status == "contradicted"]
    ```

## Full schema

??? abstract "schemas/run-verdict.schema.json"

    ```json
    --8<-- "schemas/run-verdict.schema.json"
    ```
