---
description: Install Evidence Judge, record an agent run with Playwright, judge it and read the report.
---

# Getting started

Four steps from nothing to a verdict you can trust. Each takes a few minutes, and none of them
needs an API key.

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } __1. Install__

    ---

    Install the CLI with uv, pip or Docker, and check it runs.

    [:octicons-arrow-right-24: Install](install.md)

-   :material-record-rec:{ .lg .middle } __2. Record a run__

    ---

    Turn on Playwright tracing (and a HAR) around your agent, and save what it said.

    [:octicons-arrow-right-24: Record a run](record-a-run.md)

-   :material-gavel:{ .lg .middle } __3. Judge it__

    ---

    Run `evidence-judge judge` on the recording, with and without a task spec.

    [:octicons-arrow-right-24: Your first judgement](first-judge.md)

-   :material-file-chart-outline:{ .lg .middle } __4. Read the report__

    ---

    Understand the verdict, the claims table, citations, dimensions and exit codes.

    [:octicons-arrow-right-24: Read the report](read-the-report.md)

</div>

!!! tip "Judging a coding agent instead?"

    [Judge a coding-agent session](judge-a-session.md) does the same for a Claude Code or Codex
    session: it checks the agent's answer against the commands it ran, their output and the
    files it edited, on a transcript that ships with the repository.

!!! tip "No agent yet? Use the recorded demo runs"

    The repository ships a small shop app and seven recorded agent runs in
    `tests/fixtures/runs/`: an honest success, an honest failure, a forged success, a constraint
    violation, a side effect, a click loop and a do-nothing agent. Every page in this section
    works on them, so you can follow along before wiring up your own agent.

    Two more demo sites, `demo/clinic` (an appointment form) and `demo/ledger` (an admin
    console), have eight recorded runs in `tests/fixtures/sites/`, each with its spec. A booking
    the clinic confirmed:

    ```bash
    evidence-judge judge --offline \
      --trace tests/fixtures/sites/clinic_booked/trace.zip \
      --answer-file tests/fixtures/sites/clinic_booked/answer.txt \
      --spec tests/fixtures/sites/clinic_booked/spec.yaml
    ```

    It passes: both key claims (the booking request and the reference HD-1001 on the
    confirmation page) and all four spec checks are verified against the recording.
