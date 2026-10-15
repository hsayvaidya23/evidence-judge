"""The one-shot repair loop shared by every structured-output backend: an invalid reply is retried
once with the validation error appended to the prompt."""

from __future__ import annotations

from pydantic import ValidationError

MAX_ERROR_CHARS = 2_000


class InvalidOutputError(Exception):
    """The reply did not satisfy the schema; the message says why, for the repair prompt."""


def with_error(prompt: str, error: str) -> str:
    return (
        f"{prompt}\n\n<previous_attempt_error>\n{error}\n</previous_attempt_error>\n"
        "Your previous attempt was rejected for the reason above. "
        "Answer again with corrected output."
    )


def format_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return "Schema validation failed: " + "; ".join(parts)
