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
site: https://shop.example.com      # the site the task is on (URL or host[:port])
expect: [...]                       # expectations that must be met
forbid: [...]                       # expectations that must NOT be met
constraints: [...]                  # value predicates that must hold
allow: [...]                        # request predicates for expected intermediate mutations
allowed_hosts: [api.example.com]    # more hosts of the site; '*.example.com' for every subdomain
failure_markers: [...]              # regexes that mark a page a redirect lands on as a failure
```

Each entry in `expect` and `forbid` is an expectation:

```yaml
- id: order_submitted                  # required
  description: An order request succeeded
  all: [<predicate>, ...]              # every predicate must hold
  any: [<predicate>, ...]              # at least one must hold
  max_count: 1                         # optional, see "Repeated mutations"
```

An expectation needs at least one predicate in `all` or `any`. When both are given, both
conditions apply. In the verdict each one becomes an `ExpectationResult` with `met: true`,
`false`, or `null` when it cannot be decided: the channel it needs was not captured, or what
meets it proves nothing (see below).

### Which hosts can prove something

Evidence that *proves* something (an agent's claim or an `expect` entry) must come from the
site the run was about. Name it with `site`; `--site` on the command line overrides it.

A site is a host and a port, whatever the scheme:

- `site` is a URL (`https://shop.example.com/`) or a host name with an optional port
  (`shop.example.com`, `127.0.0.1:8000`, `localhost:3000`). Ports 80 and 443 are the default
  port; any other port must be named, and then only that port is the site:
  `https://shop.example.com:8443` is not on a site named `https://shop.example.com`, and a site
  named `127.0.0.1` does not cover `http://127.0.0.1:8000`.
- A site covers its own host with or without a leading `www.` (`www.shop.example.com` and
  `shop.example.com` are one site), and **no subdomains**. List each other host the site uses
  (an API host, a checkout host) in `allowed_hosts`, or name every subdomain of a host with a
  wildcard: `*.example.com` covers `api.example.com` and `a.b.example.com`, but not
  `example.com` itself. Wildcards are only allowed in `allowed_hosts`.
- `site`, `--site` and every `allowed_hosts` entry are checked when the spec loads (an invalid
  spec or `--site` exits with code 3): the scheme must be http or https; user names and
  passwords (`https://user:pw@shop.example.com`) are refused. A site is a whole host, so a
  path, query or fragment is refused too (`github.com/myorg`): every page on a host can prove
  the task, so a site cannot be narrowed to part of one. Name the host alone only when all of
  it belongs to the site under test, and require the pages you mean with `url` predicates in
  `expect`. Public suffixes and single-label names other than `localhost` (`com`, `co.uk`,
  `github.io`, `myshopify.com`, `fly.dev`, `s3.amazonaws.com`, `intranet`) are refused,
  because anyone can register a site under them; `my-store.myshopify.com` is a site. Hosts are
  lower-cased, a trailing dot is dropped, and international names are IDNA-encoded:
  `shöp.example` is stored as `xn--shp-tna.example` and matches pages recorded under either
  form. The public-suffix check uses the full [Public Suffix List](https://publicsuffix.org/)
  (ICANN and private sections) bundled with the `publicsuffixlist` package; it never fetches
  anything at run time, and upgrading that package refreshes the list.

With neither `site` nor `allowed_hosts` (nor `--site`), the site is inferred from the run's
first page, host and port: the initial page state, else the first page the agent opened. The
agent chooses that page, so an agent that opens `https://evil.test/` first and posts its "order"
to `https://evil.test/api/orders` would prove its own task, and one that reads its answer off a
page on `https://evil.test/` would prove a read-only task. **A run never passes on an inferred
site**, whether its task writes or only reads. With a spec, a run that would pass gets

```
NEEDS_REVIEW: the site host (shop.example.com) was inferred from the run's first page, which the
agent may have chosen; set `site` in the task spec (or pass --site) so evidence from another
host cannot prove the task.
```

and without a spec, however many hosts the run visited,

```
NEEDS_REVIEW: no site was named; pass --site or set `site` in a spec.
```

- With `site` or `allowed_hosts` set, request, page text, URL and value predicates only match
  requests and page states on the site: a `GET` to another host or port proves nothing either.
- With the site inferred, a mutation (POST, PUT, PATCH, DELETE) and every page state (page text,
  URLs, values read with `page_regex`) must be on the inferred site. A POST to a third party
  (`https://httpbin.org/anything/order`) cannot prove "order placed", and it is reported as a
  side effect; a confirmation shown on another host (a gist or paste page the agent opened)
  cannot prove it either: a `final`-scope predicate on such a final page is `no_evidence`, and
  an `any`-scope one skips such pages. Other reads (a `GET` request) on any host still count
  until the site is named.
- A mutation outside the site and the allowed hosts is a side effect, unless an `allow` entry
  names its host (see [Payment providers, APIs and CDNs](#payment-providers-apis-and-cdns)).
- A request that meets every criterion except the host is `no_evidence`, never a contradiction:
  the judge cannot tell a third party from a sibling API host it was not told about. Its reason
  names the host (`POST /api/orders → 201 is a POST request ... but it went to api.example.com,
  outside the run's host (shop.example.com) ...; if the site owns api.example.com (an api. or
  static. subdomain), list it in the spec's allowed_hosts`), the claim or `expect` entry stays unproven and the run goes to
  review. Sibling hosts and subdomains are not matched by registrable domain (`example.com`);
  list them, or use a `*.` wildcard.
- Page states whose URL is `data:`, `about:`, `blob:` (or `javascript:`, `chrome:`,
  `devtools:`) never count: an agent can fill such a page with any text. A `final`-scope
  predicate on such a final page is `no_evidence`.

`forbid` entries and `constraints` are not restricted: a forbidden request or a violating value
counts wherever it went.

### Payment providers, APIs and CDNs

A task often touches hosts other than the site. Which spec field covers one depends on who owns
it:

- **A host the site owns** (`api.shop.example.com`, `static.shop.example.com`, a checkout
  subdomain): list it in `allowed_hosts`. It becomes part of the site, so its requests and
  pages can prove claims and `expect` entries, and its mutations are judged like the site's.
- **A third party the task uses** (a payment provider's 3-D Secure callback, an address
  lookup API, a CDN): do **not** list it in `allowed_hosts`, because then its pages and
  requests could prove the task: a provider page that says "Payment successful" would pass a
  checkout the shop never confirmed. Explain its mutations with an `allow` entry whose
  `url_contains` names the host. An `allow` entry explains a mutation to another host only
  when it names that host, so `url_contains: [/3ds]` does not cover
  `hooks.payprovider.test/3ds/authenticate`, and neither does `/api/cart` cover a POST to a
  mirror at `shop.example.com:8443/api/cart`.

GETs to a third party (scripts, fonts, a CDN) need nothing: reads are not side effects.

A worked example: the shop answers the order with a redirect to the provider's 3-D Secure
challenge, the agent enters the code, and the provider posts it and redirects back to the
shop's confirmation page:

```
POST https://shop.test/checkout/submit           → 303 to hooks.payprovider.test/3ds/challenge
GET  https://hooks.payprovider.test/3ds/challenge → 200  "Verify it's you. Enter the code."
POST https://hooks.payprovider.test/3ds/authenticate → 302 to shop.test/order/A1/confirmed
GET  https://shop.test/order/A1/confirmed         → 200  "Thank you for your order! Order A1 ..."
```

With only `site: shop.test`, the provider's POST is a side effect, and the report says how to
explain it:

```
NEEDS_REVIEW: side effect: net#8: POST /3ds/authenticate → 302 went to hooks.payprovider.test,
outside the site (shop.test), so no expectation can explain it; if hooks.payprovider.test is a
third party the task uses (a payment provider, an API, a CDN), explain it with an allow entry
such as `allow: [{kind: request, method: POST, url_contains: [hooks.payprovider.test/3ds]}]`;
list it in allowed_hosts only if the site owns it (an api. or static. subdomain), since pages
and requests on an allowed host can prove the task.
```

Following the hint, this spec passes the honest run, and only the shop's own order request
and confirmation page prove it:

```yaml
id: buy-kettle
task: Buy a red kettle under $30
site: shop.test
expect:
  - id: order
    all:
      - {kind: request, method: POST, url_contains: [/checkout/submit]}
  - id: confirmation
    any:
      - {kind: page_text, contains: Thank you for your order}
allow:
  - {kind: request, method: POST, url_contains: [/api/cart]}
  - {kind: request, method: POST, url_contains: [hooks.payprovider.test/3ds]}
```

The confirmation entry also decides what the redirected order request proves (see
[What a mutation proves](#what-a-mutation-proves)): the shop's page says `Thank you for your
order`, so its footer `If your card is declined later, we will email you` does not send the run
to review.

### Forbidden attempts

A `forbid` entry also catches attempts that did not succeed. Inside `forbid`, a `request`
predicate whose status is `ok` (the default), `2xx` or `null` is checked twice:

- a matching request that its status accepts breaks the rule (`met: false`, the run fails). With
  the default `ok`, that includes a 3xx: a forbidden `POST` answered `303` took effect;
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
true. Telemetry (pings, beacons, CSP reports) is not a side effect, unless its body carries
redacted values or, with `allowed_hosts` set, goes outside the site and the allowed hosts
(possible data exfiltration); see [architecture.md](architecture.md#side-effects-and-telemetry) for the exact
rule.

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
  - {kind: request, method: POST, url_contains: [/api/coupons], max_count: 1}
```

### Repeated mutations

An `expect` entry explains only as many mutations as it needs: by default one mutation that
took effect and matches its request predicates (or a value predicate's `in_request`). An agent
that places the same order four times meets `order_submitted`, but the other three orders are
reported as a side effect (`duplicate POST /api/orders → 201, sent 4 times`), so the run needs
review. Set `max_count: N` on the expectation when the task needs N such requests.

Setting `max_count` explicitly also makes it part of the expectation: more matching mutations
than that break it (`met: false`, the run fails), which is how to say "exactly once" when a
duplicate must fail rather than go to review. Failed or rejected attempts (4xx, 5xx, no
response) are never counted.

`allow` entries explain any number of matching mutations (an agent may add to the cart several
times) unless the entry sets `max_count`; past it, the extra ones are duplicates too.

### Settings and other edits

A task such as "change my display name to Jane Doe" cannot be proven by the page: the agent
typed `Jane Doe`, so a settings page shows it as an echo of the agent's own input whether or
not the change was saved, and a `page_text` for the new value never proves anything (see
[echoes](concepts/claims-and-predicates.md)). What proves it is the write that carried the new
value to the site. Expect that request, with the value in its body:

```yaml
id: rename
task: Change my display name to Jane Doe
site: shop.example.com
expect:
  - id: saved
    all:
      - {kind: request, method: POST, url_contains: [/api/profile], body_contains: [Jane Doe]}
```

Offline, the heuristic claim extractor reads "changed / updated / set / renamed the X to Y" in
the agent's answer the same way: the claim `The agent set the display name to Jane Doe` is
proven by a `POST`, `PUT` or `PATCH` whose body contains every word of the value (`Jane` and
`Doe`, so a JSON body and a form-encoded `name=Jane+Doe` both match). An agent that typed the
name and never saved gets the claim contradicted. Vague values (`to the new one`), secrets
that ingest redacts (passwords, card numbers) and values without a word of three or more
letters (`to 2`) make no such claim. Use `PUT` or `PATCH` in the spec when that is what the
site sends.

## Predicates

Every predicate has a `kind`.

Predicates that every run would satisfy are rejected when the spec or claims file is loaded,
with a message naming the field. Strings are stripped; substrings (`url_contains`,
`path_contains`, `query_contains`, `body_contains`, `response_contains`, a url `contains`, an
action's `target_contains`, and a value compared with `contains`) need at least 2 characters;
page text needs at least 3 characters including a letter or digit (so `"24"` or `"!!!"` is
refused); a request needs a method or a URL, path, query, regex or body criterion; a url
predicate needs `contains`, `query_contains` or `regex`; an action needs a name, target or
value; a value predicate needs `json_path` or `page_regex`.

Malformed values are refused at load time too, so a broken spec exits with code 3 before any
run is judged: a `status` outside the accepted values (see below), a regex that does not compile
(`url_regex`, a url `regex`, `page_regex`), a `page_regex` without exactly one capture group, a
`json_path` that is not a dotted path (`items.0.price`, `$.items[0].price`), and a numeric
`value` that is not finite (`.nan`, `.inf`).

Inside `expect`, a request predicate needs both a `method` and at least one of `url_contains`,
`path_contains`, `query_contains`, `url_regex`, `body_contains` or `response_contains`, because
"some POST succeeded" proves nothing. `forbid` and `allow` may stay broad, as in the read-only
example below.

Claims (pre-extracted or produced by an extractor) are held to a stricter bar, because the agent
controls what they say: besides the request rule above, an action needs a target or value, and
page text needs two words or a number (`"1042"`, `"$24.00"`, `"Thank you for your order"`), since
a lone word such as `confirm` also labels buttons on pages that prove nothing. A claims file that
breaks these rules is refused; the extractors drop such predicates and count them as
`vacuous_predicates` in the claim set's coverage note, and a key claim left without predicates
is `no_evidence`, so the run goes to review. When a run is judged, a claim predicate that the
run already met before the agent acted, that only holds on an echo of the agent's own input
(text it typed or put in a URL), or (for a key outcome claim) that is neither a mutation
request nor the final page cannot support the claim either. The same baseline and echo rules
hold for `expect` entries, whatever wrote the spec: an `expect` predicate the run met before the
agent acted, or one that only holds where a page, URL or response echoes the agent's input (an
agent that opens `/order/A77/confirmed` itself, searches for "Order A77 shipped", or makes a
search API answer `{"query": "In stock"}`), leaves the entry unknown (`met: null`) and the run
goes to review. `forbid` entries and `constraints` are not masked: forbidden text the agent
typed or that was already shown still breaks the rule. See
[evidence that proves nothing](concepts/claims-and-predicates.md#evidence-that-proves-nothing).

A `url` entry is never met by a URL the agent typed itself, even one the site answered with a
2xx and even when the entry only means "the agent visited this page": a single-page app or a
soft-404 page answers every path with 200, so `/products/red-kettle` typed into the address bar
loads whether or not that product exists, and a `contains` needle also matches a crafted
`/search/products/red-kettle`. The judge cannot tell a visit entry from an outcome entry
(`/order/A77/confirmed`), so it treats both the same. An agent that reaches the page by
clicking a link, or that the site redirects there, meets the entry; to accept typed
navigation, prove the visit with what the page shows (`page_text`) instead of its URL.

### `request`: a network request was observed

| Field | Default | Meaning |
| --- | --- | --- |
| `method` | any | HTTP method, e.g. `POST` |
| `url_contains` | `[]` | every substring must appear in the scheme, host or path (the query string and fragment are ignored) |
| `path_contains` | `[]` | every substring must appear in the URL path |
| `query_contains` | `[]` | every substring must appear in the decoded query string |
| `url_regex` | none | regular expression the full URL (query included) must match |
| `status` | `"ok"` | `ok` (2xx or 3xx, and the request did not fail), exact code (`"201"`), class (`"2xx"`), `none` (no response), `any` (anything, with or without a response), or `null` (any response) |
| `body_contains` | `[]` | every substring must appear in the request body (case-insensitive) |
| `response_contains` | `[]` | every substring must appear in the response body |

```yaml
- {kind: request, method: POST, url_contains: [/api/orders], status: 2xx}
- {kind: request, method: GET, path_contains: [/search], query_contains: [q=red kettle]}
```

The default `ok` accepts redirects because a form that posts and then redirects
(POST/Redirect/GET) answers the `POST` with `303` and the browser loads the result page with a
`GET`. Write `status: 2xx` or an exact code when a redirect must not count. `null` needs a
response of any status; a request that never got one (a connection error, or one the browser
blocked) only matches `none` or `any`. A status that is none of these is refused when the spec
loads.

#### What a mutation proves

When a mutation is to *prove* something (a claim or an `expect` entry), matching it is not
enough: a failed write is often answered like a successful one. The request is `no_evidence`,
and the run goes to review, when:

- **its response reports a failure.** A `2xx` whose JSON (or form-encoded) body has a
  top-level `errors` or `error` that holds something, an `ok`, `success` or `succeeded` that is
  false, or a `status`, `state`, `result` or `outcome` that names a failure (`failed`,
  `declined`, `payment_refused`, ...). A GraphQL `{"data": {"placeOrder": null}, "errors":
  [{"message": "Payment declined"}]}` answered `200` reads `... but it was answered 200 and its
  response reports an error ('Payment declined'), with 'placeOrder' null`.
- **it was redirected to a failure.** A failed form is often redirected too (`POST
  /api/orders → 302`, then `GET /cart?error=card_declined`), so a 3xx is judged by the page it
  led to: the page load matching its `Location` header, else the next page load, and what that
  page showed before the browser moved on (its snapshots, or the final state if the run ended
  there). In order:
    1. The page must load with a 2xx.
    2. Its decoded path and query must not mark a failure. URL markers are whole words:
       `error`, `failed`, `declined`, `refused`, `rejected`, `cancelled`, `invalid`, `denied`,
       `unauthorized`, `expired`, `insufficient`, `try-again`, `problem`, `unable`,
       `unsuccessful`, `timeout`, `timed-out`, `not-processed`, and a few common words in
       other languages (`refusé`, `abgelehnt`, `fehlgeschlagen`, `Fehler`, `rechazado`,
       `rifiutato`, `échec`). A status-like query field (`status`, `state`, `result`,
       `outcome`, or a name ending in one, such as `paymentStatus`) whose whole value is `ko`
       or `nok` marks a failure, and so does a success flag (`ok`, `success`, `paid`,
       `approved`, ...) set to `false`, `0` or `no`. A query field left empty or `0`, like
       `?errors=0`, marks nothing.
    3. Neither its URL nor its text may match one of the spec's `failure_markers` (below).
    4. If the page is on the site and shows a page text that an `expect` entry asks for
       (`Thank you for your order`), that positive evidence decides: the mutation is proven,
       whatever else the page says. A confirmation page with a footer such as `If a payment is
       declined later, we will email you` passes.
    5. Otherwise the whole page text is scanned for a failure message (`declined`, `refused`,
       `Something went wrong`, `Please try again`, `could not be placed`, ...; negations such
       as `No errors` are ignored). Recorded page text is flat, so nothing tells a footer from
       the message; a landing page that says `Your card was declined` must not prove the
       order, so the scan reads all of it, and a footer that mentions a failure sends the run
       to review. Name the confirmation text in an `expect` entry and step 4 applies instead.
    6. A redirect back to the page the request was sent from (a settings form that saves in
       place) counts only when that page's text after the redirect was recorded and differs
       from before.

    A failed check reads like `... but it was answered 302 and led to GET
    /cart?error=card_declined → 200, whose URL marks a failure ('error')`.

A URL `expect` entry does not stand in for step 4: an order page such as `/order/A1` is
reached whether or not its payment went through. A `forbid` entry or a constraint skips all of
this: a forbidden `POST` answered with any 3xx, or with a `200` that reports errors, happened.

#### `failure_markers`: failures in your site's words

The built-in markers are mostly English. List the words or messages your site shows when the
task fails, in any language, as `failure_markers`: case-insensitive regular expressions matched
against the decoded URL and the text of the page a redirect lands on. A match always makes the
mutation `no_evidence`, even when the page also shows the expected confirmation text.

```yaml
failure_markers:
  - Zahlung\s+(wurde\s+)?abgelehnt     # "payment declined"
  - paiement\s+refus                    # "paiement refusé"
  - estado=rechazado
```

`url_contains` ignores the query string so that `POST /api/log?next=/api/orders` cannot pass for
an order request. Use `query_contains` to match query parameters, or `url_regex` for the whole
URL.

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
- {kind: url, contains: /search, query_contains: q=red kettle}
- {kind: url, regex: '/orders/\d+$', scope: any}
```

`contains` matches the scheme, host and path only, like a request's `url_contains`, so a final
URL of `/login?next=/order/confirmation` does not count as reaching `/order/confirmation`.
`query_contains` matches the decoded query string, and `regex` the full URL. When several are
given, all must hold.

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

Instead of `json_path`, `page_regex` extracts the value from page text with exactly one capture
group:

```yaml
- {kind: value, field: total, page_regex: 'Total:\s*\$([\d.,]+)', op: "<=", value: 30}
```

`page_regex` reads **every** match on **every** recorded page state (each snapshot and the final
page), and every value it reads must satisfy the comparison: one violation makes the predicate
`contradicted`, so a total of `$45.00` on the checkout page is not hidden by a `$5 off` coupon on
the confirmation page. When the values differ but all satisfy it, the reason says so. Anchor
the pattern on a label such as `Total:`: a bare price pattern like `\$([\d.]+)` also reads
shipping fees, coupons and every other product listed on the way.

Numbers are read from JSON numbers and from strings as prices are usually written: currency
symbols or codes (`$24.00`, `1500 EUR`), thousands grouped with commas, dots, spaces or
apostrophes (`24,000.50`, `1 500`, `1'500`, `1.500,00 €`), a decimal comma before one or two
digits (`24,5`) and a `k`/`m`/`b` suffix. Ambiguous strings such as `1.500` are not numbers.
`<`, `<=`, `>` and `>=` need numbers on both sides; if either side is not one, the predicate is
`no_evidence` ("cannot compare ... numerically"), never a text comparison. `==` and `!=` fall
back to comparing normalised text.

Without `in_request`, the value is looked up in every state-changing request that took effect
(answered 2xx or 3xx and not failed); a rejected or failed attempt does not count. With
`in_request`, its own `status` decides (the default `ok` likewise skips failed attempts).
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

### Regular expressions

`url_regex`, `regex` and `page_regex` come from specs or from an LLM, so they are treated as
untrusted. They are matched case-insensitively with the [`regex`](https://pypi.org/project/regex/)
engine, which accepts Python `re` syntax (lookarounds and backreferences included) and supports
a per-match timeout. A pattern that does not compile, or is longer than 300 characters ("unsafe
regex"), is refused when the spec or claims file loads, and an extractor drops it. Every other
valid pattern runs, but each search
stops after 50 ms: a pattern that backtracks catastrophically on the evidence, such as `(a+)+$`
on a long run of `a`, makes the predicate `no_evidence` with the reason "regex timed out", and a
spec `allow` entry whose regex times out explains no mutation.

A regex reads at most the first and the last 10,000 characters of a URL or a page's text.

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
      - {kind: request, method: POST, url_contains: [/api/orders]}
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
      - {kind: request, method: POST, status: any}
      - {kind: request, method: PUT, status: any}
      - {kind: request, method: DELETE, status: any}
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
