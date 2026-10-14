# Security

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/hsayvaidya23/evidence-judge/security/advisories/new),
not as public issues. Include the version (`evidence-judge version`), a description, and a
minimal reproduction **with all real data removed**. You can expect an acknowledgement within
five working days.

Supported versions: the latest release on PyPI.

## What Evidence Judge handles

Agent recordings are sensitive. A Playwright trace or HAR file routinely contains session
cookies, bearer tokens, API keys, form submissions (including passwords), personal data and
payment details. Evidence Judge reads these files locally and, when an LLM is configured, sends
selected evidence to the model provider.

## Redaction

Redaction is **on by default** (`EVJ_REDACT=true`). It runs immediately after ingest, before
claim extraction, before any LLM call and before any report is written. It covers:

| Category | What is redacted |
| --- | --- |
| `auth` / `bearer` / `jwt` | `Authorization` and `Proxy-Authorization` credentials, bearer tokens, JWTs anywhere |
| `cookie` | Every value in `Cookie` / `Set-Cookie` (cookie names are kept) |
| `secret_header` | `X-API-Key`, `X-Auth-Token` and headers whose names contain token, secret, session, auth, password, api-key |
| `api_key` | Common key formats: `sk-...`, `ghp_...`, `github_pat_...`, `AKIA...`, `xox[bp]-...`, `AIza...`, Stripe keys, GitLab tokens |
| `secret_param` | Secret-looking URL query, fragment, form and JSON fields (`token`, `password`, `secret`, `api_key`, `client_secret`, ...; in URLs also `key`, `code`, `session`, `sig`) |
| `url_password` | Passwords in `user:password@host` URLs |
| `secret_input` | Values typed into fields whose selector suggests a password, OTP, PIN or card security code |
| `email` | Email addresses |
| `card` | Payment card numbers (issuer prefix + Luhn checked) |

This applies to request and response bodies, headers, URLs, page snapshot text and HTML, console
messages, action values, run metadata, and the agent's answer and reasoning. User-supplied task
specs and claim files are redacted the same way so their predicates still match.

Each value becomes `[REDACTED:<category>:<fingerprint>]`, where the fingerprint is the first
8 hex characters of an HMAC-SHA256 of the value. Equal values get equal tokens, so a claim that
quotes an email still matches the request that sent it, without the email ever being shown.
Set `EVJ_REDACTION_KEY` to keep fingerprints stable across runs; otherwise a random key is used
per process. Treat the key as a secret: with it, a fingerprint can be confirmed by guessing.

### Limits

- **Screenshots are not redacted.** Pixels can show anything on screen. They are embedded in
  HTML reports and, for screenshot checks, sent to the vision model.
- Redaction is pattern-based. Secrets in unusual formats (custom tokens, names, addresses,
  phone numbers) may pass through. Review reports before sharing them.
- `--no-redact` (or `EVJ_REDACT=false`) disables all of the above. Use it only on synthetic data.

## Rules for contributors and users

- **Never commit traces, HAR files or screenshots that contain real secrets or personal data.**
  Fixtures must be synthetic. `.gitignore` excludes `out/` and `.evidence-judge/`; keep
  recordings there.
- Never commit `.env`. API keys are read from the environment only.
- Agent output is untrusted input. The HTML report escapes all agent-controlled text and ships a
  Content-Security-Policy that blocks scripts and external requests; console output never
  interprets agent text as markup. Treat any way around this as a vulnerability.
