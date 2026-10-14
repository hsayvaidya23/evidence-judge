---
description: Record an agent run with Playwright tracing and a HAR file, and save the agent's answer and reasoning for Evidence Judge.
---

# Record a run

Evidence Judge grades what was **recorded**, so the first job is to record. The best recording is
a Playwright trace plus a HAR file from the same browser context.

## With Playwright

Wrap your agent in tracing. This is how the demo fixtures are recorded
(`scripts/record_fixtures.py`):

```python
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path("runs/checkout")
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(record_har_path=out / "run.har")  # (1)!
    context.tracing.start(screenshots=True, snapshots=True)  # (2)!

    answer, reasoning = run_my_agent(context)  # your agent drives pages in this context

    context.tracing.stop(path=out / "trace.zip")
    context.close()  # (3)!
    browser.close()

(out / "answer.txt").write_text(answer + "\n", encoding="utf-8")
(out / "reasoning.txt").write_text("\n\n".join(reasoning) + "\n", encoding="utf-8")  # (4)!
```

1.  The HAR fills in request and response bodies the trace may have missed. It is optional but
    recommended.
2.  `snapshots=True` records DOM snapshots (page text and URLs); `screenshots=True` records
    screencast frames. Screenshots are never redacted, so see
    [Redaction & privacy](../concepts/redaction.md) before turning them on for real accounts.
3.  Playwright writes the HAR when the context closes.
4.  Reasoning files hold one step per paragraph, separated by blank lines.

Supported trace formats are Playwright trace versions 7 to 10, and HAR 1.2.

## What to save

| File | Flag | What it is |
| --- | --- | --- |
| `trace.zip` | `--trace` | Actions, network, DOM snapshots, screenshots, console |
| `run.har` | `--har` | Request and response bodies the trace missed |
| `answer.txt` | `--answer-file` | What the agent **claims**: its final answer |
| `reasoning.txt` | `--reasoning-file` | The agent's reasoning, one step per paragraph (optional) |
| `spec.yaml` | `--spec` | What success must look like ([task specs](../task-specs.md)); write it once per task |

You can pass the answer inline with `--answer "..."` instead of a file. If you already have
claims, `--claims claims.json` skips extraction entirely.

!!! note "Other agent frameworks"

    browser-use histories are read directly with `--browser-use`. For anything else, record a
    Playwright trace or a HAR around the browser your agent drives. See the
    [recording recipes](../guides/recording-recipes.md).

## Check the recording

Before judging, look at what was actually captured:

```console
$ evidence-judge inspect --trace runs/checkout/trace.zip --har runs/checkout/run.har
run trace  source playwright-trace+har
Evidence
┏━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┓
┃ channel     ┃ items ┃ captured ┃
┡━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━┩
│ actions     │     6 │ yes      │
│ network     │     7 │ yes      │
│ mutations   │     2 │ yes      │
│ snapshots   │     7 │ yes      │
│ screenshots │    11 │ yes      │
│ console     │     1 │ yes      │
└─────────────┴───────┴──────────┘
State-changing requests
┏━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ id    ┃ method ┃ status ┃ url                                 ┃
┡━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ net#3 │ POST   │ 200    │ http://127.0.0.1:56432/api/cart     │
│ net#6 │ POST   │ 201    │ http://127.0.0.1:56432/api/orders   │
└───────┴────────┴────────┴─────────────────────────────────────┘
final http://127.0.0.1:56432/orders/1001  (Order #1001 | Kettle Co.)
```

That is the `honest_success` demo run. A channel marked `captured: no` matters: claims that need
it can only be left unverified, never contradicted
([the absence rule](../concepts/evidence-channels.md)).

!!! warning "Mismatched HAR"

    If a HAR shares none of the trace's requests (or fewer than half of them, for a trace with 5
    or more requests), loading fails with exit code `3` instead of merging another run's traffic.
    Check you passed the right files; `--allow-mismatch` merges them anyway.

Next: [your first judgement](first-judge.md).
