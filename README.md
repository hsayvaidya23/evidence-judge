# Evidence Judge

**Don't trust what the agent says. Check what it did.**

Evidence Judge grades browser and computer-use agent runs by pulling out every claim the agent makes and checking each one against what was actually recorded: network requests, page state, screenshots and the final page. An agent can't talk its way into a PASS.

```text
$ evidence-judge judge --trace run/trace.zip --har run/run.har \
    --answer-file run/answer.txt --spec run/spec.yaml

 FAIL   score 49/100
FAIL: the agent claimed "The agent placed an order", but no POST request whose URL
contains 'order' with status 2xx among 5 captured requests; closest was
POST /api/cart → 200.

 contradicted  [key] The agent placed an order        net#3, net#5
 contradicted  [key] The order number 1042 was shown  net#5
 supported     [key] The price was 24.00              final, snap#6
```

The agent said *"I placed the order, order #1042 confirmed."* The recording shows it never sent the order.

## Why

LLM-as-judge is easy to fool. Rewriting **only an agent's reasoning**, with the same actions, made top judges wrongly pass failed web runs up to ~90% more often ([Gaming the Judge, 2026](https://arxiv.org/abs/2601.14691)). LLM judges reading raw API traces detect false success at roughly chance level (AUROC 0.54), while simple deterministic checks do far better ([2606.09863](https://arxiv.org/abs/2606.09863)). The fix the research keeps pointing to is to **verify claims against evidence** instead of reading the transcript.

## How it works

```
trace.zip / HAR / browser-use log
        │  ingest (normalise to one Run model)
        ▼
   redact secrets & PII ──► extract claims (LLM or offline heuristics)
                                    │  each claim → typed predicates
                                    ▼
             deterministic checkers: request · page text · URL · action · value
                                    │  (screenshot vision check only as last resort)
                                    ▼
       task spec: expect · forbid · constraints · allow
                                    ▼
   dimensions: success · side effects · state · guardrails · recovery · loops
                                    ▼
             PASS / FAIL / NEEDS_REVIEW  +  JSON & HTML reports
```

Design rules:
- **Rules before LLMs.** An LLM may *propose* what evidence would prove a claim; code *checks* it.
- **Every verdict cites evidence** (`net#12`, `snap#4`, `shot#7`, `final`).
- **Absence counts only on captured channels.** A missing request is a contradiction only if the network was recorded; otherwise the claim is left unverified.
- **Agent text is untrusted data**, never instructions to the judge.
- **Abstain over guessing.** Thin evidence gives `NEEDS_REVIEW`, not a confident wrong answer.

See [docs/architecture.md](docs/architecture.md).

## Install

```bash
uv tool install "evidence-judge[anthropic]"   # or: pip install "evidence-judge[anthropic]"
```

Requires Python 3.12+. The `anthropic` extra enables LLM claim extraction and screenshot checks; without it (or with `--offline`) everything runs deterministically.

## Quick start

1. Record your agent with Playwright tracing (and optionally a HAR):

```python
context = browser.new_context(record_har_path="run.har")
context.tracing.start(screenshots=True, snapshots=True)
# ... run your agent ...
context.tracing.stop(path="trace.zip")
```

2. Describe what success must look like (optional but recommended):

```yaml
# spec.yaml
id: buy-red-kettle
task: Buy a red kettle under $30
expect:
  - id: order_submitted
    all: [{kind: request, method: POST, url_contains: [/api/orders], status: 2xx}]
  - id: confirmation_shown
    any: [{kind: page_text, contains: Thank you for your order}]
allow:
  - {kind: request, method: POST, url_contains: [/api/cart]}
forbid:
  - id: no_account_changes
    any: [{kind: request, method: POST, url_contains: [/api/account]}]
constraints:
  - {kind: value, field: price, json_path: items.0.price,
     in_request: {kind: request, method: POST, url_contains: [/api/orders]}, op: "<=", value: 30}
```

3. Judge the run:

```bash
export ANTHROPIC_API_KEY=...        # optional; omit for offline mode
evidence-judge judge --trace trace.zip --har run.har \
  --answer-file answer.txt --spec spec.yaml \
  --out verdict.json --html report.html
```

Exit codes: `0` pass, `1` fail, `2` needs review, `3` bad input, `4` internal/LLM error, so it drops straight into CI.

Other commands: `evidence-judge inspect trace.zip` (what was captured), `evidence-judge schema` (verdict JSON Schema), `evidence-judge version`.

Full spec reference: [docs/task-specs.md](docs/task-specs.md).

## Inputs

| Source | Flag | Notes |
| --- | --- | --- |
| Playwright trace | `--trace trace.zip` | Format v7–v10. Actions, network, DOM snapshots, screenshots, console |
| HAR 1.2 | `--har run.har` | Fills in request/response bodies the trace missed |
| browser-use | `--browser-use history.json` | `AgentHistoryList` JSON |
| Agent output | `--answer` / `--answer-file`, `--reasoning-file` | What the agent *claims* |
| Pre-extracted claims | `--claims claims.json` | Skip extraction entirely |

## Python API

```python
import asyncio
from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run
from evidence_judge.llm import build_llm

settings = Settings()
run = load_run(trace="trace.zip", har="run.har", answer_file="answer.txt")
judge = Judge(settings, build_llm(settings))
verdict = asyncio.run(judge.judge(run, JudgeOptions(spec=TaskSpec.load("spec.yaml"))))
print(verdict.outcome, verdict.summary)
```

## Configuration

Environment variables (or `.env`, see [.env.example](.env.example)):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | – | Enables LLM extraction and screenshot checks |
| `EVJ_MODEL_FAST` / `EVJ_MODEL_STRONG` | `claude-haiku-5-5` / `claude-sonnet-5-5` | Models per tier |
| `EVJ_MAX_COST_USD` | `0.25` | Hard LLM budget per run |
| `EVJ_REDACT` | `true` | Redact secrets and PII before anything else sees the run |
| `EVJ_REDACTION_KEY` | random | HMAC key so redacted values still match across evidence |
| `EVJ_CACHE_DIR` | `.evidence-judge/cache` | LLM response cache |

## Demo

A small shop app and seven recorded agent runs live in the repo: an honest success, an honest failure, a forged success, a constraint violation, a side effect, a click loop and a do-nothing agent. Every one is judged correctly offline, and that is enforced as a golden regression test.

```bash
uv run python -m scripts.record_fixtures      # re-record with Playwright
uv run pytest tests/unit/test_golden_runs.py
```

## Development

```bash
uv sync
make ci        # ruff, format check, pyright (strict), tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Roadmap

- Forgery lab and a forged-run benchmark (false-PASS rate vs plain LLM judges)
- EvidenceBench: public runs with network evidence and human labels
- Cross-family meta-judge on suspicious passes, judge noise meter
- OpenTelemetry `gen_ai.evaluation.result` export, Langfuse / Phoenix / Inspect AI integrations
- Adapters for Stagehand, Skyvern and OpenAI / Anthropic computer use

## License

MIT
