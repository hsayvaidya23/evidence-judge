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
selected, redacted evidence text to the model provider. Screenshots are only sent when you opt in
with `EVJ_SCREENSHOTS_TO_LLM=true` (see Limits).

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
| `html_secret` | The `value` of an `<input>` in page or body HTML that is `type="password"`, has a password or one-time-code `autocomplete`, or whose `name`/`id` contains a password, token, secret, OTP, PIN, CVV or CSRF word (hidden CSRF/auth tokens included) |
| `email` | Email addresses |
| `card` | Payment card numbers (issuer prefix + Luhn checked) written in card groups (`4111 1111 1111 1111`), or as bare digits when the field name (`card_number`, `pan`, `cc`) or nearby words (card, payment, visa, ...) say it is a card. A card number seen once in a run is redacted wherever it repeats. Bare numbers in order, tracking or reference id fields are left alone |

This applies to request and response bodies, headers, URLs, page snapshot text and HTML, console
messages, action values, run metadata, and the agent's answer and reasoning. Percent-encoded
values (`jane.doe%40example.com` in a form body, URL path or query) are decoded, checked and
re-encoded. User-supplied task specs and claim files are redacted the same way so their
predicates still match.

Each value becomes `[REDACTED:<category>:<fingerprint>]`, where the fingerprint is the first
8 lowercase hex characters of an HMAC-SHA256 of the value's canonical form: emails are
case-folded and card numbers reduced to their digits, because evidence matching ignores case and
card formatting; credentials are fingerprinted exactly. Equal values get equal tokens, so a claim
that quotes an email still matches the request that sent it, without the email ever being shown,
and redaction never changes a verdict (this is tested). Set `EVJ_REDACTION_KEY` to keep
fingerprints stable everywhere; otherwise, when a run uses the LLM cache, a random key is kept in
`<EVJ_CACHE_DIR>/redaction.key` (mode 0600), and in every other case a random key is used per
process. Treat the key and the key file as secrets: with the key, a fingerprint can be confirmed
by guessing.

### Limits

- **Screenshot pixels are never redacted.** Redaction works on text; a screenshot shows
  whatever was on screen, including typed passwords left visible, personal data and card
  numbers. Screenshots are therefore **not sent to the model provider by default**
  (`EVJ_SCREENSHOTS_TO_LLM=false`; screenshot checks then report no evidence). If you set it to
  `true`, `judge` prints a notice and a claim that reaches the screenshot check sends the
  screenshot, unchanged, to the vision model. HTML reports always embed screenshots unchanged.
  To keep sensitive pixels out:
  - Record with sensitive fields masked. For explicit Playwright screenshots pass
    `mask=[locator, ...]` (and `style=` to hide elements); trace screencast frames cannot be
    masked, so hide sensitive elements with injected CSS (`page.add_style_tag`) or record the
    trace with `tracing.start(screenshots=False)`.
  - Run agents against test accounts and synthetic data when the recording will be judged with
    a hosted model or the report will be shared.
  - Use `--offline` to judge without any model call, so no screenshot leaves the machine, and
    treat HTML reports from real sessions as sensitive.
- Redaction is pattern-based. Secrets in unusual formats (custom tokens, names, addresses,
  phone numbers) may pass through. Review reports before sharing them.
- `--no-redact` (or `EVJ_REDACT=false`) disables all of the above. Use it only on synthetic data.

## Untrusted recordings

Recordings are treated as untrusted input, since they may come from a third party or a
compromised agent:

- **File references stay inside the recording.** A browser-use `screenshot_path` and a HAR
  `_file` body are read only when they resolve, after following symlinks, inside the history or
  HAR directory (for browser-use, or an explicit `screenshot_root`). Screenshots must also be
  regular files of at most 10 MB that start with PNG, JPEG, WebP or GIF magic bytes. Anything
  else is skipped with a warning in the run metadata.
- **Trace archives are size-checked before they are inflated.** Using the sizes the zip
  declares (which `zipfile` never inflates past), a trace fails with `TraceTooLargeError` when
  an event file exceeds 200 MB, the archive exceeds 1 GB uncompressed or 100,000 entries, or an
  entry over 1 MiB compresses more than 200:1. Bodies and screenshots over 10 MB are skipped
  with a warning. Pass `limits=TraceLimits(...)` to `load_playwright_trace` to change these.
- **HAR and browser-use files are size-capped.** A HAR over 200 MB or a browser-use history over
  50 MB fails with `TooLargeError` without being read past the cap (`max_file_bytes=` on
  `load_har` / `load_browser_use_history` changes this). `TraceTooLargeError` is a
  `TooLargeError` too.
- **Memory stays bounded.** Event files are streamed line by line, only the part of a body that
  `max_body_chars` keeps is read, and trace screenshots are read from the zip on demand.
- **Mismatched evidence is refused.** When a HAR shares none of a trace's requests, or fewer
  than half of them for a trace with 5 or more requests, loading fails with
  `MismatchedEvidenceError` instead of merging another run's traffic into the evidence. Requests
  only the HAR saw are listed in `metadata["har_only_requests"]`, and HAR-only requests starting
  more than 10 minutes outside the trace's time window are dropped with a note in
  `metadata["merge_warnings"]`. `allow_mismatch=True` (CLI `--allow-mismatch`) overrides all of
  this.
- Every ingest failure is an `IngestError` (a `ValueError`), reported by the CLI as bad input
  (exit code 3).

## Rules for contributors and users

- **Never commit traces, HAR files or screenshots that contain real secrets or personal data.**
  Fixtures must be synthetic. `.gitignore` excludes `out/` and `.evidence-judge/`; keep
  recordings there. The LLM cache and redaction key live outside the repository, in the per-user
  cache directory (`EVJ_CACHE_DIR`).
- Never commit `.env`. API keys are read from the environment only.
- Agent output is untrusted input. The HTML report escapes all agent-controlled text and ships a
  Content-Security-Policy that blocks scripts and external requests; console output never
  interprets agent text as markup. Treat any way around this as a vulnerability.
