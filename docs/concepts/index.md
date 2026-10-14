---
description: The ideas behind Evidence Judge - claims, predicates, evidence channels, task specs, dimensions and outcome rules.
---

# Concepts

Evidence Judge answers one question about an agent run: **is what the agent says supported by
what the browser recorded?** It does not read the answer and decide whether it sounds right. It
turns the answer into checkable claims and checks each one against evidence.

``` pipeline
flowchart LR
  REC["Recording<br/>trace · HAR · browser-use"] --> RUN["Run<br/>normalised events"]
  RUN --> RED["Redact<br/>secrets & PII"]
  RED --> CLM["Claims<br/>verbatim quotes"]
  CLM --> PRD["Predicates<br/>request · url · text"]
  PRD --> CHK["Checkers<br/>code, with citations"]
  CHK --> EXP["Spec<br/>expect · forbid"]
  EXP --> DIM["Dimensions<br/>six scores"]
  DIM --> OUT(["Outcome<br/>+ score"])
  classDef verdict stroke-width:2px
  class OUT verdict
```

## Six design rules

1. **Rules before LLMs.** Anything that can be decided from a request, a status code, a URL or
   page text is decided by code. An LLM may *propose* what evidence would prove a claim; code
   *checks* it. The screenshot check is the last resort.
2. **Every verdict cites evidence.** A claim is never `supported` or `contradicted` without
   citations to specific events (`net#12`, `snap#4`, `final`).
3. **Absence counts only on captured channels.** "The agent never submitted the form" is a
   contradiction only if the network was recorded.
4. **Agent text is untrusted.** The answer and reasoning are data, never instructions: delimited
   in prompts, escaped in HTML, plain text in the terminal.
5. **Abstain over guessing.** Thin evidence gives `NEEDS_REVIEW`, not a confident wrong verdict.
6. **Redact before anything leaves the process.** Redaction runs before extraction, before any
   LLM call and before any report.

## In this section

<div class="grid cards" markdown>

-   [__Claims & predicates__](claims-and-predicates.md)

    What a claim is, the six predicate kinds, and how predicate results combine into a claim
    verdict.

-   [__Evidence channels__](evidence-channels.md)

    What each recording source captures, and the absence rule.

-   [__Task specs__](../task-specs.md)

    `expect`, `forbid`, `constraints`, `allow` and `allowed_hosts`.

-   [__Dimensions__](dimensions.md)

    Success, side effects, state, guardrails, recovery and loops.

-   [__Outcomes & rules__](outcomes.md)

    The four rules that turn everything into PASS, FAIL or NEEDS_REVIEW.

-   [__Redaction & privacy__](redaction.md)

    What is redacted, how fingerprints still match, and what is not covered.

-   [__LLM vs offline__](llm-vs-offline.md)

    What the model does, what it never does, and what changes offline.

-   [__Architecture__](../architecture.md)

    Modules, stages and key types.

</div>
