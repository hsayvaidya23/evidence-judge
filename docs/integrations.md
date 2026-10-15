# Integrations

Evidence Judge can send each verdict to the tools you already use to watch agents: OpenTelemetry,
Langfuse, Arize Phoenix and Inspect AI. Each integration is an optional extra, so the core install
stays small:

```bash
pip install "evidence-judge[otel]"       # OpenTelemetry API + SDK
pip install "evidence-judge[langfuse]"   # Langfuse Python SDK
pip install "evidence-judge[phoenix]"    # arize-phoenix-client (no pandas needed)
pip install "evidence-judge[inspect]"    # Inspect AI
pip install "evidence-judge[all]"        # all of the above plus anthropic and mcp
```

!!! info "Not released yet"

    v0.1.0 is not released yet, so nothing is on PyPI: install from the GitHub repository, e.g.
    `uv tool install --python 3.12 "evidence-judge[otel] @ git+https://github.com/hsayvaidya23/evidence-judge"`
    (see [Install](getting-started/install.md)). The commands above will work once it is on
    PyPI.

To let a coding agent call Evidence Judge as tools, see the [MCP server](guides/mcp.md) guide
(`evidence-judge[mcp]`).

Using an integration without its extra raises `MissingExtraError` (an `ImportError`) naming the
extra to install. In the CLI, `--otel` and `--langfuse-trace-id` are exports you asked for, so a
missing extra (or, for `--otel` without a configured tracer provider, a missing OTLP exporter)
or missing Langfuse credentials exit with code 3 before anything is judged. Once judging has
run, a failed export is only a warning and never changes the verdict's exit code.

All integrations export the same results:

| Result | Name | Score | Label | Explanation |
| --- | --- | --- | --- | --- |
| Run | `evidence_judge.outcome` | verdict score, 0..1 | `pass` / `fail` / `needs_review` | summary |
| Claim | `evidence_judge.claim` | none | `supported` / `contradicted` / `no_evidence` | reason |
| Dimension | `evidence_judge.dimension.<name>` | dimension score, or none | `not_assessable` when unscored | findings |

Dimension names are `success`, `side_effects`, `state`, `guardrails`, `recovery` and `loops`.
Claims carry their id, key flag, kind, confidence, checker and cited evidence ids (`net#3`,
`snap#6`, ...). Every result carries `run_id`, `schema_id` and `judge_version`.

The CLI and the Inspect scorer redact runs before judging, so exported reasons and summaries
contain redaction tokens, not raw secrets. When you call `Judge` yourself, redact the run first
(`Redactor.redact_run`) if the verdict will leave the process.

## OpenTelemetry

Each verdict becomes one short span, `evidence_judge.evaluate`, carrying one
[`gen_ai.evaluation.result`](https://github.com/open-telemetry/semantic-conventions-genai) event
per run, claim and dimension:

| Attribute | Value |
| --- | --- |
| `gen_ai.evaluation.name` | `evidence_judge.outcome`, `evidence_judge.claim`, `evidence_judge.dimension.<name>` |
| `gen_ai.evaluation.score.value` | score, when there is one |
| `gen_ai.evaluation.score.label` | outcome, claim status or `not_assessable` |
| `gen_ai.evaluation.explanation` | summary, reason or findings |
| `error.type` | only on `emit_error`, for runs that could not be judged |
| `evidence_judge.*` | `run_id`, `schema_id`, `judge_version`, `claim_id`, `key`, `kind`, `confidence`, `checker`, `evidence_ids`, `dimension` |

The GenAI evaluation conventions are at **Development** status and now live in
`open-telemetry/semantic-conventions-genai`. The attribute names are pinned as constants in
`evidence_judge/integrations/otel.py` so a rename is a one-line change.

The convention asks for the event to be parented to the span that was evaluated. Pass that span
and the evaluation span becomes its child and links to it, so the results land in the agent's
trace:

```python
from opentelemetry import trace
from evidence_judge.integrations.otel import emit_verdict

tracer = trace.get_tracer("my-agent")
with tracer.start_as_current_span("agent.run") as agent_span:
    ...  # run the agent, record trace.zip
    verdict = ...  # judge it

# the judged span as a context ...
emit_verdict(verdict, parent_context=trace.set_span_in_context(agent_span))
# ... or as hex ids, e.g. read from your trace store
emit_verdict(verdict, trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7")
```

Only the OpenTelemetry API is used to emit, so the events go wherever your application's tracer
provider sends them. Pass `tracer=` to use a specific one.

From the CLI, `--otel` uses the globally configured tracer provider (for example under
`opentelemetry-instrument`). When none is configured, it builds one from the standard `OTEL_*`
environment variables with an OTLP exporter, and exits with code 3 before judging if that
exporter is not installed. A W3C `TRACEPARENT` environment variable names the judged span:

```bash
pip install "evidence-judge[otel]" opentelemetry-exporter-otlp
export OTEL_SERVICE_NAME=agent-evals
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export TRACEPARENT=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01   # optional
evidence-judge judge --trace trace.zip --answer-file answer.txt --otel
```

`OTEL_EXPORTER_OTLP_PROTOCOL=grpc` switches to the gRPC exporter, and `OTEL_SDK_DISABLED=true`
turns export off.

## Langfuse

Scores are attached to a Langfuse trace, or to one observation in it:

| Score | Type | Value | Comment |
| --- | --- | --- | --- |
| `evidence_judge.score` | `NUMERIC` | 0..1 | summary |
| `evidence_judge.outcome` | `CATEGORICAL` | `pass` / `fail` / `needs_review` | summary |
| `evidence_judge.dimension.<name>` | `NUMERIC` | 0..1, assessable dimensions only | findings |

Score ids are deterministic (UUIDv5 of run id, score name and target trace/observation), so judging
the same run again updates the scores instead of adding duplicates. Score metadata holds `run_id`,
`schema_id` and `judge_version`.

```python
from langfuse import get_client
from evidence_judge.integrations.langfuse import push_to_langfuse

langfuse = get_client()  # reads LANGFUSE_* env vars
push_to_langfuse(verdict, trace_id=langfuse_trace_id, observation_id=None, client=langfuse)
langfuse.flush()
```

Without `client`, a `Langfuse()` client is built from `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` and flushed before returning. From the CLI:

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com
evidence-judge judge --trace trace.zip --answer-file answer.txt \
  --langfuse-trace-id 4bf92f3577b34da6a3ce929d0e0e4736
```

Missing credentials exit with code 3 before judging. A failed push is printed as a warning and
does not change the verdict's exit code.

## Arize Phoenix

Verdicts become span annotations on the agent's span: one for the outcome, one per claim (pass
`include_claims=False` to skip them) and one per dimension. Each annotation has an `identifier`
(`outcome`, `claim:c1`, `dimension:success`, ...), so logging again for the same span replaces the
earlier annotations. The annotator kind is `CODE` when no model call shaped the verdict, otherwise
`LLM`.

```python
from phoenix.client import Client
from evidence_judge.integrations.phoenix import push_to_phoenix

push_to_phoenix(verdict, span_id="00f067aa0ba902b7", client=Client())  # PHOENIX_* env vars
```

To send them yourself, build the records (`SpanAnnotationData` dicts) or flat rows for a DataFrame;
neither needs pandas:

```python
from evidence_judge.integrations.phoenix import span_annotation_rows, span_annotations

client.spans.log_span_annotations(span_annotations=span_annotations(verdict, span_id=span_id))

import pandas as pd

client.spans.log_span_annotations_dataframe(
    dataframe=pd.DataFrame(span_annotation_rows(verdict, span_id=span_id))
)
```

## Inspect AI

`evidence_judge_scorer` grades each sample by judging the browser run its metadata points to:

```python
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from evidence_judge.integrations.inspect_ai import evidence_judge_scorer


@task
def shop_agent() -> Task:
    return Task(
        dataset=[
            Sample(
                input="Buy a red kettle under $30",
                metadata={
                    "trace": "runs/42/trace.zip",
                    "har": "runs/42/run.har",
                    "spec": "runs/42/spec.yaml",
                },
            ),
        ],
        solver=my_browser_agent(),  # writes the recording the metadata names
        scorer=evidence_judge_scorer(offline=True),
    )
```

Sample metadata keys:

| Key | Meaning |
| --- | --- |
| `trace`, `har`, `browser_use` | the recording; at least one is required |
| `spec` | task spec YAML (optional) |
| `answer` / `answer_file` | the agent's answer; defaults to the model's final completion |
| `reasoning_file` | the agent's reasoning (optional) |
| `task` | task text; defaults to the spec's task, then the sample input |
| `run_id` | run id for the verdict |

Relative paths resolve against `base_dir=` (default: the working directory).

The score value is `C` for pass, `I` for fail and `N` for needs review (choose another grade with
`needs_review="P"`), scored with `accuracy` and `stderr`. `value="score"` returns the 0..1 verdict
score instead. The explanation is the verdict summary and the score metadata is the full verdict
JSON (`evidence-judge/run-verdict@1`). Runs are redacted as in the CLI unless `redact=False` or
`EVJ_REDACT=false`. Without `offline=True`, claims are extracted with an LLM when
`ANTHROPIC_API_KEY` is set.
