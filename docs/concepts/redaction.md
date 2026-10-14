---
description: How Evidence Judge redacts secrets and PII with HMAC fingerprints before extraction, LLM calls and reports, why verdicts still match, and what redaction does not cover.
---

# Redaction & privacy

Agent recordings are sensitive. A trace or HAR routinely holds session cookies, bearer tokens, API
keys, form submissions (passwords included), personal data and payment details. Evidence Judge
reads them locally and redacts them **before anything else sees the run**.

``` pipeline
flowchart LR
  ING["Ingest"] --> RED["Redact<br/>on by default"]
  RED --> EXT["Claim extraction"]
  RED --> LLM["Any LLM call"]
  RED --> REP["JSON · HTML · console"]
  RED --> EXP["OTel · Langfuse · Phoenix"]
```

## What is redacted

| Category | What |
| --- | --- |
| `auth` / `bearer` / `jwt` | `Authorization` and `Proxy-Authorization` credentials, bearer tokens, JWTs anywhere |
| `cookie` | Every value in `Cookie` / `Set-Cookie` (names are kept) |
| `secret_header` | `X-API-Key`, `X-Auth-Token` and headers whose names contain token, secret, session, auth, password, api-key |
| `api_key` | Common key formats: `sk-...`, `ghp_...`, `github_pat_...`, `AKIA...`, `xox[bp]-...`, `AIza...`, Stripe keys, GitLab tokens |
| `secret_param` | Secret-looking URL query, fragment, form and JSON fields |
| `url_password` | Passwords in `user:password@host` URLs |
| `secret_input` | Values typed into password, OTP, PIN or card-security-code fields |
| `html_secret` | The `value` of password, one-time-code and token `<input>`s in page HTML, hidden CSRF tokens included |
| `email` | Email addresses |
| `card` | Payment card numbers (issuer prefix and Luhn checked) |

It applies to bodies, headers, URLs, snapshot text and HTML, console messages, action values,
run metadata, and the agent's answer and reasoning. Your task specs and claim files are redacted
with the same key so their predicates still match. The full list and edge cases are in
[Security](../security.md#redaction).

## Fingerprints keep verdicts stable

Each value becomes `[REDACTED:<category>:<fingerprint>]`, where the fingerprint is the first 8
hex characters of an HMAC-SHA256 of the value. Equal values get equal tokens, so a claim that
quotes an email still matches the request that sent it without the email ever being shown:

```text
"I updated the account email to [REDACTED:email:4cb5d04d] so the confirmation would reach the
right inbox."
```

**Redaction never changes a verdict**; that invariance is tested.

The HMAC key comes from `EVJ_REDACTION_KEY`; otherwise, when a run uses the LLM cache, from a
random key file at `<EVJ_CACHE_DIR>/redaction.key` (mode 0600); otherwise a per-process key.
Treat the key and the key file as secrets: with the key, a fingerprint can be confirmed by
guessing.

## What redaction does not cover

!!! danger "Screenshot pixels are never redacted"

    Redaction works on text. A screenshot shows whatever was on screen. That is why
    `EVJ_SCREENSHOTS_TO_LLM` is **off** by default (screenshot checks then report no evidence),
    and why HTML reports should be treated as sensitive: they embed screenshots as recorded.

To keep sensitive pixels out:

- Mask sensitive fields when recording: `mask=[locator, ...]` for explicit Playwright
  screenshots; for trace screencasts, hide elements with injected CSS (`page.add_style_tag`) or
  record with `tracing.start(screenshots=False)`.
- Run agents against test accounts and synthetic data when recordings leave your machine.
- Judge with `--offline`, so no model call is made at all.

Redaction is pattern-based: secrets in unusual formats, names, addresses and phone numbers may
pass through. Review reports before sharing them. `--no-redact` (or `EVJ_REDACT=false`) disables
all of it; use it only on synthetic data.
