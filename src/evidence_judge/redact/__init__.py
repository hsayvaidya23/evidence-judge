"""Secret and PII redaction for recorded evidence and agent output.

Every sensitive value is replaced by ``[REDACTED:<category>:<fingerprint>]`` where the fingerprint
is the first 8 lowercase hex characters of an HMAC-SHA256 of the value's canonical form (emails
case-folded, card numbers digits only). Equal values therefore produce equal tokens, so a claim
that quotes a redacted value still matches the redacted evidence, while the value itself never
leaves the process. Redacting already-redacted data is a no-op.
"""

from evidence_judge.redact.redactor import TOKEN_RE, RedactionReport, Redactor

__all__ = ["TOKEN_RE", "RedactionReport", "Redactor"]
