"""Export verdicts to observability and eval tools: OpenTelemetry, Langfuse, Phoenix, Inspect AI.

Each integration lives in its own module and needs its optional extra, such as ``otel``
(see the install guide). Importing this package needs none of them.
"""

from evidence_judge.integrations._common import (
    EvaluationRecord,
    MissingExtraError,
    evaluation_records,
)

__all__ = ["EvaluationRecord", "MissingExtraError", "evaluation_records"]
