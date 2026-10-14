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

### `screenshot`: a yes/no question for a vision model

```yaml
- {kind: screenshot, question: Is a red kettle shown in the cart?, scope: final}
```

This is the last resort: it needs an LLM, costs money, and is less reliable than the other
kinds. Prefer a `request`, `url` or `page_text` predicate whenever one can express the condition.

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
