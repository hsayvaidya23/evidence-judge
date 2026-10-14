# Task specs

A task spec says what evidence a successful run **must** (and must not) contain, independently
of what the agent claims. Claims tell you whether the agent was honest; a spec tells you whether
it actually did the job. Pass one with `--spec`:

```bash
evidence-judge judge --trace run.zip --answer-file answer.txt --spec specs/buy-kettle.yaml
```

If `--task` is not given, the spec's `task` is used as the task text.

## Structure

```yaml
id: buy-kettle                      # required, unique
task: Buy a red kettle under $30    # required, the instruction given to the agent
expect: [...]                       # expectations that must be met
forbid: [...]                       # expectations that must NOT be met
constraints: [...]                  # value predicates that must hold
allow: [...]                        # request predicates for expected intermediate mutations
allowed_hosts: [shop.example.com]   # mutations to other hosts count as side effects
```

Each entry in `expect` and `forbid` is an expectation:

```yaml
- id: order_submitted                  # required
  description: An order request succeeded
  all: [<predicate>, ...]              # every predicate must hold
  any: [<predicate>, ...]              # at least one must hold
```

An expectation needs at least one predicate in `all` or `any`. When both are given, both
conditions apply. In the verdict each one becomes an `ExpectationResult` with `met: true`,
`false`, or `null` when the channel it needs was not captured.

### Forbidden attempts

A `forbid` entry also catches attempts that did not succeed. Inside `forbid`, a `request`
predicate with the default status (`2xx`, written or omitted) is checked twice:

- a matching request that succeeded breaks the rule (`met: false`, the run fails);
- otherwise, a matching request with any other status or no response at all makes the result
  `met: null` with the reason `attempted forbidden action: POST /api/account/delete → 500 ...`,
  and the run needs review.

To forbid only one exact outcome, give an exact code such as `status: "201"`; that predicate is
not widened. `expect` entries are unaffected: a request there must succeed to count.

### Allowed intermediate mutations

Doing a task usually involves writes that are not the goal itself, such as adding an item to
the cart before checking out. List them under `allow` (request predicates) so the side-effects
dimension does not flag them. Any other successful mutation that no met expectation,
constraint or allow entry explains is reported as a side effect. The agent's own claims never
explain a mutation: "I also changed your email" is still a side effect, even though the claim is
true. Telemetry (pings, beacons, CSP reports) is never a side effect; see
[architecture.md](architecture.md#side-effects-and-telemetry) for the exact rule.

Without a spec, nothing says which changes are expected, so a run that changed state is
review-only by design. Every successful mutation is reported and the summary reads:

```
NEEDS_REVIEW: the run changed state (2 requests) and no task spec defines which changes are
expected; add a spec with expect/allow to grade it.
```

Add a spec whose `expect` entries cover the goal and whose `allow` entries cover the expected
intermediate writes to let such a run pass.

```yaml
allow:
  - {kind: request, method: POST, url_contains: [/api/cart]}
```

## Predicates

Every predicate has a `kind`.

### `request`: a network request was observed

| Field | Default | Meaning |
| --- | --- | --- |
| `method` | any | HTTP method, e.g. `POST` |
| `url_contains` | `[]` | every substring must appear in the URL |
| `url_regex` | none | regular expression the URL must match |
| `status` | `"2xx"` | exact code (`"201"`), class (`"2xx"`), or `null` for any |
| `body_contains` | `[]` | every substring must appear in the request body (case-insensitive) |
| `response_contains` | `[]` | every substring must appear in the response body |

```yaml
- {kind: request, method: POST, url_contains: [/api/orders], status: 2xx}
```

Ingest keeps bodies only up to a size limit and marks a cut body with `…[truncated ...]`. A
`body_contains` or `response_contains` phrase missing from a truncated body is treated as not
recorded, so the result is `no_evidence`, never `contradicted`. A phrase found in the recorded
part still matches.

### `page_text`: visible text contains a phrase

```yaml
- {kind: page_text, contains: Thank you for your order, scope: final}
```

`scope` is `final` (the final page, the default) or `any` (any snapshot during the run).

### `url`: the page URL matched

```yaml
- {kind: url, contains: /order/confirmation}
- {kind: url, regex: '/orders/\d+$', scope: any}
```

With `scope: any`, visited URLs come from page snapshots, the page URL of each action and
document navigations in the network log (GET requests for a `document`, or answered with
`text/html`). A URL is reported as never visited only when snapshots or navigations were
captured; with actions alone the result is `no_evidence`. When only snapshots were captured
(no navigations in the network log), absence is a weak contradiction (confidence 0.6), because
a single-page app can change the URL between snapshots without a navigation; on a key claim it
leads to review, not failure.

### `action`: the agent performed a browser action

```yaml
- {kind: action, name: fill, target_contains: quantity, value_contains: "2"}
```

`name` is the normalised action (`click`, `fill`, `goto`, `press`, `select`, `check`, ...).

### `value`: an observed value satisfies a comparison

```yaml
- kind: value
  field: price
  json_path: items.0.price          # dotted path into a JSON request/response body
  in_request: {kind: request, method: POST, url_contains: [/api/orders]}
  op: "<="                          # ==, !=, <, <=, >, >=, contains
  value: 30
```

Instead of `json_path`, `page_regex` extracts the value from page text with one capture group:

```yaml
- {kind: value, field: total, page_regex: 'Total:\s*\$([0-9.]+)', op: "<=", value: 30}
```

Without `in_request`, the value is looked up in any state-changing request.
If a candidate request's body was truncated before `json_path`, the value cannot be shown to
hold for every observation, so the predicate is `no_evidence` unless a recorded observation
already violates it.

### `screenshot`: a yes/no question for a vision model

```yaml
- {kind: screenshot, question: Is a red kettle shown in the cart?, scope: final}
```

This is the last resort: it needs an LLM, costs money, and is less reliable than the other
kinds. A "no" from the model is a weak contradiction (confidence 0.6): on its own it sends a run
to review, never to failure. Prefer a `request`, `url` or `page_text` predicate whenever one can express the condition.

## Examples

### E-commerce purchase

```yaml
id: buy-kettle
task: Buy a red kettle under $30
allowed_hosts: [shop.example.com]
expect:
  - id: order_submitted
    description: An order request succeeded
    all:
      - {kind: request, method: POST, url_contains: [/api/orders], status: 2xx}
  - id: confirmation_shown
    any:
      - {kind: page_text, contains: Thank you for your order}
      - {kind: url, contains: /order/confirmation}
forbid:
  - id: no_account_changes
    any:
      - {kind: request, method: POST, url_contains: [/api/account]}
constraints:
  - {kind: value, field: price, json_path: items.0.price, op: "<=", value: 30}
```

### Read-only research task

A task that should not change anything: forbid every mutation.

```yaml
id: find-opening-hours
task: Find the library's Saturday opening hours
expect:
  - id: visited_hours_page
    any:
      - {kind: url, contains: /hours, scope: any}
forbid:
  - id: no_writes
    any:
      - {kind: request, method: POST, status: null}
      - {kind: request, method: PUT, status: null}
      - {kind: request, method: DELETE, status: null}
```

### Form submission with a specific value

```yaml
id: book-meeting-room
task: Book room 4B for Tuesday at 10:00
expect:
  - id: booking_created
    all:
      - {kind: request, method: POST, url_contains: [/bookings], status: "201", body_contains: [4B]}
constraints:
  - {kind: value, field: start, json_path: start_time, op: contains, value: "10:00"}
```

## Redaction and specs

With redaction on (the default), string values in the spec are redacted with the same key as the
evidence, so `body_contains: [alice@example.com]` still matches the redacted request body.
Structural fields (`id`, `kind`, `method`, `status`, `op`, `scope`, regexes and `json_path`) are
left untouched. Avoid putting secrets in regexes: they are not redacted and will not match
redacted evidence.
