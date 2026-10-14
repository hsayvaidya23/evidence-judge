---
description: Run the Evidence Judge forgery benchmark on your own recorded, labelled agent runs and compare it with a transcript LLM judge.
---

# Benchmark your own agent

The [forged-run benchmark](../benchmark.md) is not tied to the demo shop. Point
`evidence-judge bench` at your own recorded runs and it measures, on your tasks, how often a judge
can be talked into passing a failed run.

## 1. Lay out your runs

One directory per recorded run, the same layout as `tests/fixtures/runs/`:

```text
my-runs/
├── checkout-ok/
│   ├── expected.json      {"outcome": "pass"}            required
│   ├── trace.zip          Playwright trace  ┐
│   ├── run.har            HAR               ├ at least one
│   ├── history.json       browser-use       ┘
│   ├── answer.txt         final answer                   optional
│   ├── reasoning.txt      one step per paragraph         optional
│   └── spec.yaml          task spec                      optional, enables the +spec judge
├── checkout-out-of-stock/
│   └── ...                {"outcome": "fail"}
└── ...
```

`expected.json` is your label: `pass`, `fail` or `needs_review`. Directories without it are
skipped. Label honestly: the benchmark is only as good as the labels.

## 2. Run it

```bash
evidence-judge bench my-runs --seeds 3 --out results.json --markdown results.md
```

What happens:

1. **Honest controls.** Every run, as recorded, is judged and compared with its label.
2. **Forgeries.** Every run labelled `fail` (and any run whose directory name contains `null`)
   is rewritten with each forgery kind (`progress_fabrication`, `environment_blaming`,
   `competence_boosting`, `reflective_reasoning`, plus the `style_only` control) and seeds
   `0..N-1`. Only the answer and reasoning change; the evidence stays the same, so the correct
   verdict is never PASS.
3. **Judges.** `evidence-judge+spec` (runs with a `spec.yaml`) and `evidence-judge` (no spec),
   offline by default.

The default forger is deterministic, so the same runs and seeds always give the same forgeries.

## 3. Read the results

`results.md` has the same tables as the [benchmark page](../benchmark.md):

- **False PASS on forged runs** per judge and forgery kind, with 95% Wilson intervals. Lower is
  better. NEEDS_REVIEW is reported separately and is not a PASS.
- **Accuracy on honest controls**: verdict equal to label. Higher is better.
- **Honest control verdicts** per run, with ✗ on mismatches.

A low no-spec accuracy with zero false passes usually means the judge is abstaining on write
tasks, as designed. Adding specs is what turns those abstentions into decisions.

## 4. Compare with a transcript LLM judge

```bash
export ANTHROPIC_API_KEY=...
evidence-judge bench my-runs --seeds 3 --with-llm-baseline --markdown results.md
```

`--with-llm-baseline` adds an `llm-transcript` judge: it sees the task, the action list, the
reasoning and the final answer (and with `--baseline-screenshot`, the final screenshot), never
network traffic or page text, and answers PASS or FAIL. `--online` also switches Evidence Judge
itself to LLM claim extraction. The LLM budget (`EVJ_MAX_COST_USD`) applies per judgement.

## From Python

```python
import asyncio
from pathlib import Path

from evidence_judge.bench import BenchConfig, BenchmarkRunner, load_scenarios, to_markdown
from evidence_judge.config import Settings

result = asyncio.run(
    BenchmarkRunner(Settings(), BenchConfig(seeds=3)).run(load_scenarios(Path("my-runs")))
)
print(to_markdown(result))
```

To forge a single run yourself:

```python
from evidence_judge.forgery import ForgeryKind, forge

forged = forge(run, ForgeryKind.ENVIRONMENT_BLAMING, seed=0)
```

## Exit codes

`0` on success, `3` on bad input (no labelled runs, unreadable evidence, an LLM option without a
configured LLM), `4` on internal errors.
