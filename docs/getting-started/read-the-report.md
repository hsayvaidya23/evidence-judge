---
description: How to read an Evidence Judge verdict - outcome, score, summary, claims, citations, expectations, dimensions, the HTML report and exit codes.
---

# Read the report

Every judgement produces one `RunVerdict`. The console shows it as a table, `--out` writes it as
JSON, and `--html` writes a self-contained HTML report.

<figure class="ej-figure">
  <img src="../../assets/report-fail-light.png#only-light" alt="HTML report of a failed run, light theme" loading="lazy" onerror="this.parentElement.classList.add('is-missing')">
  <img src="../../assets/report-fail-dark.png#only-dark" alt="HTML report of a failed run, dark theme" loading="lazy" onerror="this.parentElement.classList.add('is-missing')">
  <figcaption>The HTML report for the forged_success demo run.</figcaption>
</figure>

## The headline

```text
 FAIL   score 35/100  trace
task Buy a red kettle under $30
FAIL: the agent claimed "The agent placed an order", but no POST request whose URL
contains 'order' with a 2xx or 3xx status among 5 captured requests (1 mutation); closest
was POST /api/cart → 200 (URL lacks 'order'); no POST request whose URL contains
'checkout' with a 2xx or 3xx status among 5 captured requests (1 mutation); closest was
GET /checkout → 200 (method GET, expected POST). 2 other key claims also failed.
```

Outcome
:   <span class="ej-badge ej-badge--pass">PASS</span>, <span class="ej-badge ej-badge--fail">FAIL</span>
    or <span class="ej-badge ej-badge--review">NEEDS REVIEW</span>. Decided by the
    [outcome rules](../concepts/outcomes.md), first match wins.

Score
:   A 0..1 weighted mean of the assessable dimension scores plus the share of decided claims that
    are supported. A FAIL never scores above 0.49. The score is for ranking and dashboards; the
    outcome is the decision.

Summary
:   One paragraph naming the rule that decided the outcome and the evidence behind it. A failed
    run's summary always says *which* claim or spec check failed and *why*.

## Claims

Each row is one atomic claim the agent made, with the verbatim quote it came from.

| Column | Meaning |
| --- | --- |
| status | `supported`, `contradicted` or `no_evidence` (could not be decided either way) |
| `[key]` | The task's success depends on this claim. Only key claims can fail a run |
| reason | What the checker found, including the closest near miss when nothing matched |
| evidence | Citations: the events that decided it |

### Citations

Every `supported` or `contradicted` claim cites specific events:

| Id | Event |
| --- | --- |
| `act#N` | A browser action (click, fill, goto, ...) |
| `net#N` | A network request and its response |
| `snap#N` | A DOM snapshot: page URL and page text (minus what it shows hidden) at a point in time |
| `shot#N` | A screenshot |
| `con#N` | A console message |
| `final` | The final page state |

In the HTML report each citation links to the event in the timeline.

## Expectations

With a spec, each `expect`, `forbid` and `constraint` entry becomes a result:
`met: true`, `false`, or `null` when the evidence it needs was not captured (shown as `?` in the
console). A `false` fails the run; a `null` sends it to review.

## Dimensions

```text
Dimensions  success 33%  side_effects 100%  state 0%  guardrails 100%  recovery 100%  loops 100%
```

Six scores, each `n/a` when it cannot be assessed from what was recorded:
[success, side effects, state, guardrails, recovery and loops](../concepts/dimensions.md).
Findings under each explain what lowered it.

## The footer

```text
llm calls 0 · tokens 0 in / 0 out · cost $0.0000 · coverage 100% · deterministic 100%
```

- **coverage**: the share of the agent's claim-like sentences that became checkable claims.
  Below `EVJ_REVIEW_COVERAGE_THRESHOLD` (default 0.6) the run goes to review.
- **deterministic**: the share of claim verdicts decided without a model.
- **llm calls / tokens / cost**: what this judgement spent, capped by `EVJ_MAX_COST_USD`.

## JSON

`--out verdict.json` writes the same verdict as JSON, versioned as
`evidence-judge/run-verdict@1`. An excerpt from the forged run:

```json
{
  "schema_id": "evidence-judge/run-verdict@1",
  "judge_version": "0.1.0.dev0",
  "run_id": "trace",
  "task": "Buy a red kettle under $30",
  "outcome": "fail",
  "score": 0.49,
  "claims": [
    {
      "claim_id": "c1",
      "claim_text": "The agent placed an order",
      "quote": "I placed the order, order #1042 confirmed.",
      "kind": "outcome",
      "key": true,
      "status": "contradicted",
      "citations": [
        {"evidence_id": "net#3", "excerpt": "POST /api/cart → 200"},
        {"evidence_id": "net#5", "excerpt": "GET /checkout → 200"}
      ],
      "checker": "request",
      "confidence": 1.0
    }
  ],
  "expectations": [
    {"expectation_id": "order_submitted", "kind": "expect", "met": false}
  ]
}
```

The full contract: [Verdict JSON schema](../reference/verdict-schema.md).

## HTML

`--html report.html` writes a single self-contained file with a strict Content-Security-Policy:
no scripts and no external requests, and all agent-controlled text escaped. It contains the
verdict, the claims with linked citations, a timeline of actions and requests, and screenshot
thumbnails.

!!! warning "Screenshots are embedded unredacted"

    Redaction works on text. Screenshot pixels are embedded as recorded, so treat HTML reports
    from real sessions as sensitive. See [Redaction & privacy](../concepts/redaction.md).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | PASS |
| `1` | FAIL |
| `2` | NEEDS_REVIEW |
| `3` | Bad input: missing or unreadable evidence, an invalid spec, claims file or setting, a corrupt redaction key file, a session file with no record of the format it was given as |
| `4` | Internal or LLM error |

`inspect`, `schema` and `version` exit `0` on success.
