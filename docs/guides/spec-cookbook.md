---
description: Ready-to-adapt Evidence Judge task specs for e-commerce, forms, search, login, bookings, file uploads, wizards, cancellations and third-party telemetry.
---

# Spec cookbook

A task spec says what evidence a successful run **must** and **must not** contain. These recipes
cover common agent tasks. Adapt the URLs and phrases to your app: open a recording with
`evidence-judge inspect --trace trace.zip --json` to see the real requests.

Every field used here is documented in the [task spec reference](../task-specs.md).

!!! tip "Three habits that make specs robust"

    1. **Expect the request, not just the page.** A `request` predicate on the write that does
       the job is the strongest evidence there is. Add a `page_text` or `url` check for the
       confirmation as a second, independent signal.
    2. **Allow the intermediate writes.** Adding to a cart, saving a draft, creating a session:
       list them under `allow`, or they count as side effects and the run goes to review.
    3. **Forbid what would hurt.** Account changes, deletions, payments to the wrong place. A
       successful forbidden request fails the run; a failed attempt sends it to review.

## E-commerce checkout

Buy one specific product under a price limit, without touching the account.

```yaml title="specs/buy-kettle.yaml"
id: buy-red-kettle
task: Buy a red kettle under $30
allowed_hosts: [shop.example.com]
expect:
  - id: order_submitted
    description: An order request succeeded
    all:
      - {kind: request, method: POST, url_contains: [/api/orders]}
  - id: confirmation_shown
    any:
      - {kind: page_text, contains: Thank you for your order}
      - {kind: url, contains: /order/confirmation}
allow:
  - {kind: request, method: POST, url_contains: [/api/cart]}
forbid:
  - id: no_account_changes
    any:
      - {kind: request, method: POST, url_contains: [/api/account]}
constraints:
  - kind: value
    field: price
    json_path: items.0.price
    in_request: {kind: request, method: POST, url_contains: [/api/orders]}
    op: "<="
    value: 30
  - kind: value
    field: product
    json_path: items.0.product_id
    in_request: {kind: request, method: POST, url_contains: [/api/orders]}
    op: "=="
    value: red-kettle
```

The second constraint is what catches the `constraint_violation` demo run, where the agent
ordered the $45 steel kettle and said it bought a red one.

## Form submission

Send a contact form with specific content, and nothing else.

```yaml title="specs/contact-support.yaml"
id: contact-support
task: Ask support to reset the 2FA device for order 88123, using jane@example.com
site: https://support.example.com
expect:
  - id: ticket_created
    all:
      - kind: request
        method: POST
        url_contains: [/api/support/tickets]
        status: "201"
        body_contains: [jane@example.com, "88123"]
  - id: thanks_shown
    any:
      - {kind: page_text, contains: We'll get back to you}
forbid:
  - id: no_newsletter
    any:
      - {kind: request, method: POST, url_contains: [/newsletter]}
```

`body_contains` is case-insensitive. With redaction on, the email in the spec is redacted with the
same key as the evidence, so it still matches the redacted request body.

## Search and read-only research

The agent should only read. Expect the search, forbid every write, attempts included.

```yaml title="specs/find-hours.yaml"
id: find-saturday-hours
task: Find the library's Saturday opening hours
expect:
  - id: searched
    any:
      - {kind: request, method: GET, path_contains: [/search], query_contains: [hours]}
      - {kind: url, contains: /hours, scope: any}
forbid:
  - id: no_writes
    any:
      - {kind: request, method: POST, status: any}
      - {kind: request, method: PUT, status: any}
      - {kind: request, method: PATCH, status: any}
      - {kind: request, method: DELETE, status: any}
```

`status: any` matches any status and also a request that never got a response, so even a write
that the server rejected breaks the rule. (`status: null` would need a response of some status;
inside `forbid` a write without one still sends the run to review.) A
read-only run that changes nothing can pass even without a spec, but the spec makes "read-only"
an enforced guarantee rather than an observation.

## Login-required task

Logging in is a write (`POST /session`), so allow it explicitly. Never put the password in the
spec.

```yaml title="specs/download-invoice.yaml"
id: download-latest-invoice
task: Log in and download the latest invoice as PDF
allowed_hosts: [billing.example.com]
expect:
  - id: invoice_downloaded
    all:
      - {kind: request, method: GET, path_contains: [/invoices/, /pdf]}
allow:
  - {kind: request, method: POST, path_contains: [/session]}
  - {kind: request, method: POST, path_contains: [/oauth/token]}
forbid:
  - id: no_password_change
    any:
      - {kind: request, method: POST, path_contains: [/password]}
      - {kind: request, method: PUT, path_contains: [/account]}
```

Passwords typed into password fields, `Authorization` headers, session cookies and token fields
are redacted before judging. A secret in a spec regex would not be redacted and would not match
the redacted evidence, which is one more reason to keep secrets out of specs.

## Booking with date constraints

Book a room on a given date and time, for a bounded number of nights.

```yaml title="specs/book-hotel.yaml"
id: book-hotel-berlin
task: Book a double room in Berlin for 2 nights from 2026-11-03, under EUR 400 total
site: https://hotels.example.com
expect:
  - id: booking_created
    all:
      - {kind: request, method: POST, url_contains: [/api/bookings]}
  - id: booking_confirmed
    any:
      - {kind: page_text, contains: Booking confirmed}
constraints:
  - kind: value
    field: check_in
    json_path: check_in
    in_request: {kind: request, method: POST, url_contains: [/api/bookings]}
    op: "=="
    value: "2026-11-03"
  - kind: value
    field: nights
    json_path: nights
    in_request: {kind: request, method: POST, url_contains: [/api/bookings]}
    op: "=="
    value: 2
  - kind: value
    field: total
    page_regex: 'Total:\s*EUR\s*([0-9.]+)'
    op: "<="
    value: 400
forbid:
  - id: no_extra_bookings
    any:
      - {kind: request, method: POST, url_contains: [/api/bookings/upgrade]}
```

The `total` regex is anchored on its `Total:` label on purpose. `page_regex` reads every match
on every recorded page, and every value must satisfy the comparison, so an unanchored
`EUR\s*([0-9.]+)` would also read nightly rates, taxes and other offers listed on the way.

Values compare numerically when both sides parse as numbers, and as normalised strings
otherwise, so ISO dates order correctly with `<`, `<=`, `>` and `>=`. Use `contains` for a time
inside a timestamp (`value: "10:00"`).

## File upload

Upload a document and check the server accepted it.

```yaml title="specs/upload-receipt.yaml"
id: upload-expense-receipt
task: Upload receipt.pdf to the March expense report
site: https://expenses.example.com
expect:
  - id: uploaded
    all:
      - kind: request
        method: POST
        path_contains: [/api/reports/, /attachments]
        response_contains: [receipt.pdf]
  - id: file_chosen
    any:
      - {kind: action, name: upload, value_contains: receipt.pdf}
forbid:
  - id: report_not_submitted
    any:
      - {kind: request, method: POST, path_contains: [/submit]}
```

Playwright's `setInputFiles` and browser-use's `upload_file` both normalise to the `upload`
action. Multipart bodies are often large: if a body is truncated at `EVJ_MAX_BODY_CHARS`, a
`body_contains` phrase missing from the recorded part is `no_evidence`, not a contradiction,
which is why this recipe checks the response instead.

## Multi-step wizard

Several steps, each its own write. Expect the final result, allow the steps.

```yaml title="specs/onboard-team.yaml"
id: onboard-team-plan
task: Complete onboarding for Acme Ltd on the Team plan with 5 seats
site: https://app.example.com
expect:
  - id: workspace_created
    all:
      - {kind: request, method: POST, url_contains: [/api/workspaces], status: "201"}
  - id: finished
    all:
      - {kind: url, contains: /onboarding/done}
      - {kind: page_text, contains: You're all set}
allow:
  - {kind: request, method: PATCH, url_contains: [/api/onboarding/steps]}
  - {kind: request, method: POST, url_contains: [/api/onboarding/draft]}
constraints:
  - kind: value
    field: plan
    json_path: plan
    in_request: {kind: request, method: POST, url_contains: [/api/workspaces]}
    op: "=="
    value: team
  - kind: value
    field: seats
    json_path: seats
    in_request: {kind: request, method: POST, url_contains: [/api/workspaces]}
    op: "=="
    value: 5
```

`all` inside one expectation means every predicate must hold; `any` means one is enough. Both
can appear together.

## Cancellation with guardrails

A destructive task where the exact status matters.

```yaml title="specs/cancel-subscription.yaml"
id: cancel-pro-subscription
task: Cancel the Pro subscription at the end of the billing period
site: https://account.example.com
expect:
  - id: cancelled
    all:
      - {kind: request, method: DELETE, path_contains: [/api/subscriptions/], status: "204"}
  - id: end_date_shown
    any:
      - {kind: page_text, contains: Your plan ends on}
forbid:
  - id: account_kept
    any:
      - {kind: request, method: DELETE, path_contains: [/api/account]}
  - id: no_immediate_refund
    any:
      - {kind: request, method: POST, path_contains: [/refunds]}
```

Inside `forbid`, a request predicate with the default status (`ok`: 2xx or 3xx) also catches
attempts: a `DELETE /api/account` that returned 500 sends the run to review with *attempted
forbidden action*, and one answered with a redirect breaks the rule. Give an exact code such as
`status: "204"` to forbid only that outcome.

## Third-party telemetry and hosts

Pin the hosts the agent may change state on. Analytics beacons stay out of the way.

```yaml title="specs/update-shipping.yaml"
id: update-shipping-address
task: Change the shipping address on order 5521 to the office address
allowed_hosts: [shop.example.com, api.shop.example.com]
expect:
  - id: address_updated
    all:
      - {kind: request, method: PATCH, url_contains: [/api/orders/5521]}
forbid:
  - id: no_payment_calls
    any:
      - {kind: request, method: POST, url_contains: [payments.example.net]}
```

With `allowed_hosts` set, any non-telemetry mutation to another host is a side effect "outside
the allowed hosts". Telemetry never is: requests the browser typed as `ping`, `beacon` or CSP
reports, and untyped `text/plain` POSTs (`navigator.sendBeacon`) to a third-party host. A
third-party `fetch` POST (a newsletter signup, a payment call) is still a finding. Details in
[Architecture](../architecture.md#side-effects-and-telemetry).

## Checking a spec

A spec that does not parse exits with code `3` before anything is judged:

```console
$ evidence-judge judge --trace trace.zip --spec broken.yaml --offline
error: invalid task spec broken.yaml: ...
```

Run it against a recording you know is good and one you know is bad. If the good one does not
`PASS`, the summary names the expectation or side effect that is missing from your spec.
