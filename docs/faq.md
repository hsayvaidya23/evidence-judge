---
description: Frequently asked questions about Evidence Judge - LLM judges, NEEDS_REVIEW, specs, offline mode, privacy, supported agents and CI.
---

# FAQ

??? question "How is this different from an LLM-as-judge?"

    An LLM judge reads the transcript and decides whether it sounds like success. That is exactly
    what a confident, well-written story can exploit: rewriting only an agent's reasoning, with
    the same actions, made top judges wrongly pass failed web runs up to ~90% more often
    ([Gaming the Judge](https://arxiv.org/abs/2601.14691)). Evidence Judge extracts the claims and
    checks each one against the recording with code. The agent's wording cannot change what the
    network log says.

??? question "Do I need an API key?"

    No. Without `ANTHROPIC_API_KEY`, or with `--offline`, claims are extracted by heuristics and
    every check is deterministic. The benchmark and the golden regression tests run that way. An
    LLM improves claim extraction for free-form answers; it never grades. See
    [LLM vs offline](concepts/llm-vs-offline.md).

??? question "Why did my run get NEEDS_REVIEW instead of PASS?"

    The summary names the reason. The most common ones:

    - **No spec on a write task.** Without a spec, every state change is a possible side effect,
      so a run that changed state is review-only by design. Add a spec with `expect` and `allow`.
    - **A key claim could not be verified**, usually because the channel it needs (often the
      network) was not captured. Record a HAR.
    - **Low claim coverage.** Less than `EVJ_REVIEW_COVERAGE_THRESHOLD` (60%) of the answer
      became checkable claims.
    - **An unexplained side effect**: a successful mutation your spec does not `expect` or
      `allow`.
    - **No site named.** A run only passes when the spec's `site` (or `allowed_hosts`) or
      `--site` says which site the task is on, with or without a spec: otherwise the site would
      be whatever page the agent opened first. Name the port too when it is not 80 or 443
      (`--site 127.0.0.1:8000`), and list subdomains the site uses in `allowed_hosts`.

    The full list is on [Outcomes & rules](concepts/outcomes.md).

??? question "Can I make NEEDS_REVIEW fail my CI job?"

    Yes. The CLI exits `2` on NEEDS_REVIEW, so any non-zero check fails the step. With the GitHub
    Action, set `fail-on-review: "true"`.

??? question "Do I have to write a spec for every task?"

    For tasks that change state (ordering, booking, submitting), yes, if you want them to PASS:
    nothing else says which changes were supposed to happen. Read-only tasks can pass without
    one. A spec is written once per task, not per run. The [cookbook](guides/spec-cookbook.md)
    has nine to start from.

??? question "Which agents are supported?"

    Any agent whose run you can record as a Playwright trace (format v7–v10), a HAR 1.2 file or
    a browser-use history. There is no native adapter for Stagehand, Skyvern or OpenAI /
    Anthropic computer use yet; they are on the roadmap. See
    [Recording recipes](guides/recording-recipes.md).

??? question "Does my data leave my machine?"

    Offline: no. With an LLM configured, selected evidence **text**, redacted, is sent to the
    model provider for claim extraction. Screenshots are sent only if you set
    `EVJ_SCREENSHOTS_TO_LLM=true`, because pixels are never redacted. See
    [Redaction & privacy](concepts/redaction.md).

??? question "Can an agent prompt-inject the judge?"

    Agent text is treated as data everywhere. In LLM prompts it is wrapped in delimited blocks
    with tag lookalikes neutralised; in HTML it is escaped under a CSP that blocks scripts; in
    the terminal it is never interpreted as markup; in the GitHub Action, workflow commands are
    suspended while it can reach the log. And the model only proposes claims: code decides them.

??? question "What does the score mean?"

    A 0..1 weighted mean of the dimension scores and claim support, useful for dashboards and
    ranking runs. The **outcome** is the decision; a FAIL never scores above 0.49. See
    [Dimensions](concepts/dimensions.md).

??? question "How reliable is the benchmark?"

    It is honest about its size: seventeen scenarios from fifteen recordings (two shop
    recordings are judged again against the stock check) on three small demo sites (a shop, a
    clinic's booking form and an admin console), judged against five tasks (a purchase, a
    booking, a user change, a stock check and a balance lookup), with template forgeries, so the
    intervals are wide (0 of 1494 bounds the false-PASS rate below about 0.3%, and each kind
    alone below about 10 to 14%). Offline, 5 of 51 honest controls go to review rather than
    pass: claim extraction finds no checkable claim in "I disabled Priya Shah's account".
    Run it on your own recorded runs with
    [`evidence-judge bench`](guides/benchmark-your-agent.md). Limitations are listed on the
    [benchmark page](benchmark.md#limitations).

??? question "Is it on PyPI?"

    No: v0.1.0 is not released yet. Install from the GitHub repository; see [Install](getting-started/install.md).
