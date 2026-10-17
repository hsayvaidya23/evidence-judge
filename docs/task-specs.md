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
trusted_scripts: [...]              # sha256 hashes of harness scripts that do not change the page
require_tamper_evidence: false      # true: only a Playwright trace can pass (see below)
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
- With the site inferred, a mutation (POST, PUT, PATCH, DELETE, or a GET that acts as one;
  see [What counts as a mutation](#what-counts-as-a-mutation)) and every page state (page text,
  URLs, values read with `page_regex`) must be on the inferred site. A POST to a third party
  (`https://httpbin.org/anything/order`) cannot prove "order placed", and it is reported as a
  side effect; a confirmation shown on another host (a gist or paste page the agent opened)
  cannot prove it either: a `final`-scope predicate on such a final page is `no_evidence`, and
  an `any`-scope one skips such pages. Other reads (a `GET` request) on any host still count
  until the site is named.
- A mutation outside the site and the allowed hosts is a side effect, unless an `allow` entry
  names its host or registrable domain (see [Payment providers, APIs and CDNs](#payment-providers-apis-and-cdns)).
- A request that meets every criterion except the host is `no_evidence`, never a contradiction:
  the judge cannot tell a third party from a sibling API host it was not told about. Its reason
  names the host (`POST /api/orders → 201 is a POST request ... but it went to api.example.com,
  outside the run's host (shop.example.com) ...; if the site owns api.example.com (an api. or
  static. subdomain), list it in the spec's allowed_hosts`), the claim or `expect` entry stays unproven and the run goes to
  review. Sibling hosts and subdomains are not matched by registrable domain (`example.com`);
  list them, or use a `*.` wildcard.
- Page states whose URL is `data:`, `about:`, `blob:` (or `javascript:`, `chrome:`,
  `devtools:`) never count: an agent can fill such a page with any text. A `final`-scope
  predicate on such a final page is `no_evidence`. A `view-source:` page is reported under the
  http URL of the page whose source it shows, so the `goto` that opens it counts as the agent
  writing the page instead, and evidence from then on is set aside.

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
  when it names that host or the domain it is registered under (`payprovider.test/3ds` covers
  `hooks.payprovider.test/3ds/authenticate`), so `url_contains: [/3ds]` does not cover
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

### What counts as a mutation

A request is a mutation when its method is POST, PUT, PATCH or DELETE, and also when a GET (or
any other method) says it acts as one:

- an `X-HTTP-Method-Override`, `X-HTTP-Method` or `X-Method-Override` header naming a write
  method (`X-HTTP-Method-Override: DELETE`);
- a `_method=` query parameter naming a write method (`/api/cart/1?_method=delete`, also after
  a `;` separator; the key and the value are matched in any case);
- a GraphQL GET whose `query` parameter, decoded, runs a mutation. The document is read
  definition by definition, past strings, comments, variables and directives, and fragments
  are skipped: the operation `operationName` names decides, else the only operation; when there
  are several and none is resolved, any mutation among them makes the request a write. It acts
  as a POST.

Such a request is judged like the write it names: a side effect when nothing explains it, and a
`request` predicate with `method: DELETE` (or `POST` for the GraphQL case) matches it, as does
one with `method: GET`. Reports show it as `GET as DELETE /api/cart/1`.

A plain GET that changes state (`<img src="/api/cart/delete?id=1">`, a link to `/logout`) cannot
be told apart from a read: the judge does not know the endpoint's semantics. If the site has such
endpoints, name them in a `forbid` entry, `{kind: request, method: GET, url_contains:
[/api/cart/delete]}`, so a run that calls one fails.

First-party analytics beacons are POSTs too (`navigator.sendBeacon("/collect", ...)` on every
click). A `forbid` entry that matches any POST (`{kind: request, method: POST}`) therefore fails
an honest read-only run on such a site. Scope `forbid` entries to the endpoints that matter with
`url_contains` (`/api/order`, `/cart`), and explain the beacon with an `allow` entry
(`{kind: request, method: POST, url_contains: [/collect]}`) so it is not reported as a side
effect.

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

### `trusted_scripts`: harness scripts you vouch for

A Playwright trace is read deny by default: every recorded call that is not navigation, user
interaction, waiting, reading or page and context management makes what follows the agent's
own evidence, and the run can be NEEDS_REVIEW at best (see
[evidence the agent made in the browser](concepts/claims-and-predicates.md#evidence-that-proves-nothing)).
That includes scripts a harness runs for its own reasons: a stealth `add_init_script`, a
`page.evaluate` that reads the page or scrolls. Playwright MCP's wait between actions is
recognised by itself; for any other script that does not change what the page shows, list its
hash:

```yaml
trusted_scripts:
  - 5db0ba85961130c7c6916b2714c5611bc6a0af490c664ded9a1be36483050bee
```

Get the hash from the run: `evidence-judge inspect --trace run.zip` lists every agent-made call
with the sha256 of each script, and the NEEDS_REVIEW reason prints it after the action id
(`evaluate(...) (act#3, sha256 5db0...)`). It is the SHA-256, in hex, of the UTF-8 text

```text
<api>\n<code>\n<arg>
```

where `<api>` is the call as the action names it (`evaluate`, `evaluateHandle`, `$eval`,
`$$eval`, `waitForFunction`, `addInitScript`, `addScriptTag`, `addStyleTag`, `setContent`),
`<code>` is the script exactly as the trace recorded it, and `<arg>` is its argument in
Playwright's wire form as the trace records it (`{"n":500}` for `500`, `{"v":"null"}` for none
given to `evaluate`), or `null` for a call without one, written as canonical JSON (keys
sorted, no spaces, non-ASCII kept); `evidence_judge.core.provenance.script_hash` computes it.
Only an exact match is exempt: another argument or one changed character is another hash.
Calls that are not scripts (cookies, storage, CDP sessions, WebSocket routes) have no hash and
cannot be trusted; give a harness's login state to the browser context when it is created
(`storage_state=`), which the trace does not record as a call.

### Harness setup: configure the context before tracing starts

Some browser settings change what a site serves or what the agent can read, so the trace
ingester treats a call that makes them as the agent tampering with the browser: extra HTTP
headers can carry credentials, an `X-Forwarded-For` or a feature flag the site obeys, and a
permission such as `clipboard-read` lets the agent read text it never typed. A harness that
sets them with calls during the run makes the run NEEDS_REVIEW at best, whatever it was for.
These calls taint the run when the trace records them:

| Python call | Recorded as |
| --- | --- |
| `context.set_extra_http_headers`, `page.set_extra_http_headers` | `setExtraHTTPHeaders` |
| `context.grant_permissions`, `context.clear_permissions` | `grantPermissions`, `clearPermissions` |
| `context.set_geolocation` | `setGeolocation` |
| `context.set_offline` | `setOffline` |
| `context.add_cookies`, `context.clear_cookies`, `context.set_storage_state` | `addCookies`, `clearCookies`, `setStorageState` |
| `context.add_init_script`, `page.add_init_script` | `addInitScript` (exempt only by [`trusted_scripts`](#trusted_scripts-harness-scripts-you-vouch-for)) |
| `context.route`, `page.route`, `context.route_web_socket`, `page.route_web_socket` | their handlers' `fulfill`, `continue` and `fallback` with overrides, an `abort` of anything but an image, font or media, and every WebSocket route call |
| `page.clock.*` | the clock calls |

Set them instead as options of `browser.new_context(...)` before `context.tracing.start(...)`:
the context is created before tracing begins, so its options are not recorded as calls and
the run is judged on what the agent did.

```python
context = browser.new_context(
    extra_http_headers={"Accept-Language": "en-US"},
    locale="en-US",
    timezone_id="Europe/London",
    permissions=["clipboard-read"],
    geolocation={"latitude": 51.5, "longitude": -0.1},
    storage_state="login.json",
    viewport={"width": 1280, "height": 800},
)
context.tracing.start(screenshots=True, snapshots=True)
```

Whatever is set this way is the harness's responsibility: the judge cannot see it, so a
header or a login state that makes the site lie is trusted like the site.

### Harness setup: give the agent only the traced context

A Playwright trace records the calls made on the browser context it traces, and nothing else.
An agent that holds the `Browser` can open a second context (`browser.new_context()`), or
hold the `Playwright` object and send requests with `playwright.request.new_context()` (an
`APIRequestContext`): neither is in the trace. Through one it can `POST /upgrade` or place an
order the trace never shows, so a `forbid` on that request cannot fire, and then read the
result on a page of the traced context as if the site had done it unasked. The judge cannot
tell, so the harness has to rule it out:

- Create the browser, the context and the trace in the harness, and hand the agent only that
  context or its page; never the `Browser`, the `Playwright` object, `playwright.request` or
  an `APIRequestContext`.
- If the agent's framework opens its own browser, connect it to a context the harness created
  and traces (for example over CDP to a browser the harness launched, with tracing started on
  the context the agent will use), and do not let it create others.
- Trace the context the agent works in, from before its first page: tracing a context the
  agent is not using records nothing it did.

### `require_tamper_evidence`: only a source that shows tampering can pass

Every verdict says what its evidence can show of the agent tampering with the page or its
requests (`tamper_detection` in the verdict JSON): `full` for a Playwright trace, whose call
log records injected scripts, mocked or rewritten requests and storage writes; `partial` for a
browser-use history, which records the model's actions but not what ran beneath them; `none`
for a HAR alone or any other source without a call log. A hand-written HAR looks exactly like a
recorded one. With `require_tamper_evidence: true` (or `--require-tamper-evidence`), a run
whose level is not `full` is `NEEDS_REVIEW` at best:

```yaml
require_tamper_evidence: true
```

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
(text it typed, pasted or put in a URL), or (for a key outcome claim) that is neither a mutation
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
| `body_contains` | `[]` | every substring must appear in the request body (normalised as for [`page_text`](#page_text-the-pages-text-contains-a-phrase); one with a digit only as a whole token) |
| `response_contains` | `[]` | every substring must appear in the response body (matched as `body_contains`) |
| `result_contains` | `[]` | every substring must appear in what the site answered: the response body, or the page the request led to before the agent acted again (see [what a purchase cost](concepts/claims-and-predicates.md#what-a-purchase-cost)) |

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

### `page_text`: the page's text contains a phrase

```yaml
- {kind: page_text, contains: Thank you for your order, scope: final}
```

`scope` is `final` (the final page, the default) or `any` (any snapshot during the run).

The phrase and the page are compared after normalisation: Unicode NFKC (superscript and
subscript digits are kept apart as `^4` and `_2`, so `2⁴` is not `24`), typographic quotes and
dashes as ASCII, case folded, whitespace collapsed, and invisible characters such as the soft
hyphen and zero-width space dropped. A phrase with a digit in it (an order number, a price, a
reference) must match a whole token: `1001` is not met by `10012` or `21001`, `$24` is met by
`$24.00` but not by `$24.50`, and `ACC-2041` is not met by `ACC-20411`. A space, a thin or
no-break space, or an apostrophe between digit groups joins them (`100` is not met by `1 100` or
`1'100`), and an ID of three or more digits is not met by one with a letter, or a dash and a
letter, right beside it (`1001` is not met by `1001A`, `A1001` or `1001-A`; `10:00` is met by
`10:00am`). A phrase that is only an amount of money (`$24`, `EUR 1.249,50`, `24 USD`) is met
by the same amount in the same currency however the page writes it: `$24` by `USD 24.00`,
`$ 24`, `US$24` or `24,00 $`, and `€1.249,50` by `€1,249.50`, but not by `€24` or `$24.50`. An
amount that cannot be read unambiguously (`$1.500`: 1.5 or 1500) is matched as text, and so is
one space between two digit groups with nothing to settle it (`$24 100` may be $24,100 or $24
and then 100): it meets neither `$24` nor `$24,100`. A second group or a decimal part settles
it (`$1 024 100`, `24 100,00 €`). `result_contains` also reads an amount from a JSON response
that holds it as a bare number under a price key (see
[what a purchase cost](concepts/claims-and-predicates.md#what-a-purchase-cost)). Letters without case
(Chinese, Japanese) have no word boundary the judge can see, so a phrase that ends in one is
matched as a substring at that end (`注文1001` is met by `新注文1001`, but not by `注文10012`).
Other phrases are substrings (`confirm` is met by `confirmed`). `body_contains` and `response_contains` match the
same way.

Page text is the text of the DOM snapshot the page shows. The judge reads the page's styles
(inline styles, `<style>` elements, constructed stylesheets, and the `<link rel=stylesheet>`
and `@import` sheets the trace holds, which Playwright records with the network) and sorts each
piece of text three ways:

- **Shown**: page text. Only it can meet a `page_text` predicate.
- **Hidden**, when the judge is certain: elements with the `hidden` attribute and a closed
  `<dialog>` (unless the page's own styles show them), the body of a closed `<details>` (its
  `<summary>` stays), an SVG `<title>` or `<desc>` (a tooltip), children of a shadow host that no
  `<slot>` shows, and what the cascade sets to `display: none`, `visibility: hidden` or
  `content-visibility: hidden`: `!important`, specificity, order, `@layer` order, inline
  styles, `@media` rules read at the recorded viewport and simple `@supports` tests. Each shadow
  root is its own scope: its stylesheets style only its tree, and the page's do not reach in.
- **Text the page may hide**, when visibility depends on something the judge does not fully
  model: an `@media` query it cannot read (resolution, a `calc()` with a percentage), an
  `@supports` test it does not know the answer to, `@container` or `@scope`; a selector it
  cannot evaluate (`:hover`, `:focus`, `:checked`, `:has()`, or one it cannot parse); a value
  it cannot read in any property that can hide text (an unknown function, unit or keyword,
  `attr()`, or a `var()` whose custom property it cannot tell, such as one a `:hover` rule may
  set or one an `@property` rule outside the document registers); `opacity` near 0 (its own
  and its ancestors' multiplied), a tiny `font-size`, a text colour, text fill or SVG `fill`
  with a contrast ratio below 1.2 against the background (against any stop of a background
  gradient, and any background a rule it cannot evaluate may set; an empty or zero-size image
  paints nothing, and a gradient whose colours the judge cannot tell counts as unreadable), a
  near-white or near-black text colour over a background picture (`url()`, `image-set()`)
  with no solid background colour beneath it that contrasts with the text, any `mask`, a
  `filter` other than a
  slight or luminance-keeping one, a motion path, `clip` or a `clip-path` that cuts most of a
  line of text or the viewport away (or a `path()`, `url()` or other shape the judge does not
  measure), a `transform`, `rotate` or `scale` that squashes, stretches or turns the text
  edge-on (or uses a function the judge does not know), far off-screen positions (logical
  offsets too, and a fixed box outside the viewport), margins or `text-indent`, a zero-size
  box that clips, scrolls or contains the paint of its overflow; `backface-visibility: hidden`
  on a box it or an ancestor may turn to show its back; a 3D transform under an ancestor's
  `perspective`; an `animation` (or `-webkit-animation`) whose `@keyframes` may hide text and
  that may not come to rest showing it (a fade-in that ends within five seconds at a keyframe
  that shows the text, or that fills nothing, reads as shown; one that is paused, repeats
  forever, steps, follows the scroll, runs backwards onto its hidden start, runs beside
  another animation or sets a custom property does not); a text colour an element sets for
  itself over a background its ancestor paints only on `:hover` (or in another state), unless
  a rule for that state sets the element's colour too; text under a fixed or absolute box
  with an opaque background (a colour, an opaque gradient or a `url()` picture, which counts
  as opaque) that covers the whole viewport or its containing block (`inset: 0`, or `top` and
  `left` 0 with 100% or `100vw`/`100vh` sizes) and is painted above it (a higher `z-index`,
  or a later place in the page for the same one), unless the box's own styles put it out of
  sight (slid off by its own size, scaled to nothing, clipped away, collapsed); SVG presentation attributes
  (`font-size`, `transform`, `clip-path`, `mask`, `filter`, `fill`, `opacity`, ...) count like
  the properties they name; a hiding utility class (`hidden`, `d-none`, `sr-only`, `visually-hidden`,
  `invisible`) that no recorded stylesheet defines; and all text of a page whose stylesheets
  the trace does not fully hold. The judge substitutes `var()` from the custom properties the
  cascade gives each element (with fallbacks, cycles, and `@property` initial values and
  inheritance), reads `env()` safe-area insets as 0, and evaluates `calc()`, `min()`, `max()`,
  `clamp()` and `abs()`; a percentage offset counts as a share of the viewport. It does not
  lay the page out (a snapshot records no element sizes or positions), so an opaque box smaller than its containing block that happens to sit
  over the text, or one sized another way, is not seen. Nor does it see images: a
  background picture over a solid colour is taken to look like that colour, so white text over
  a white picture on a dark `background-color` reads as shown. Values, `var()` chains, selectors and blocks nested more than 32
  deep count as unreadable.

Text the page may hide never meets a `page_text` predicate, and it keeps its absence from
contradicting one: when the phrase appears only there, the result is `no_evidence` ("text the
page may hide"), so the run goes to review instead of passing or failing. The same holds when a
page has an empty custom element (`<x-badge></x-badge>`): it may render a closed shadow root,
whose text no snapshot holds, so a phrase missing from that page is not proven missing; and
when a `::before` or `::after` rule gives a shown element text (`content: attr(data-status)`),
which no snapshot holds either; and when a shown `<iframe>` loads a document, whose text the
judge does not read. The
judge still cannot see text a script hides or that another element covers. Prefer phrases the
page shows only in the state you expect, and pair a read with a `url` or `request` predicate
when a page carries the phrase hidden.

### `url`: the page URL matched

```yaml
- {kind: url, contains: /order/confirmation}
- {kind: url, contains: /search, query_contains: q=red kettle}
- {kind: url, path: /p/red-kettle}
- {kind: url, regex: '/orders/\d+$', scope: any}
```

`contains` matches the scheme, host and path only, like a request's `url_contains`, so a final
URL of `/login?next=/order/confirmation` does not count as reaching `/order/confirmation`.
`query_contains` matches the decoded query string, `path` the whole decoded path (ignoring case
and a trailing slash), and `regex` the full URL. When several are given, all must hold.

`contains` is a substring: `contains: /p/red-kettle` is also met by `/p/red-kettle-lid` or
`/shop/p/red-kettle`. To name one page exactly, use `path: /p/red-kettle`, or a `regex`
anchored on the path's end (`regex: '/p/red-kettle/?(?:[?#]|$)'`).

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
  json_path: items.0.price          # dotted path into the request and the response body
  in_request: {kind: request, method: POST, url_contains: [/api/orders]}
  op: "<="                          # ==, !=, <, <=, >, >=, contains
  value: 30
```

`json_path` is read from **both** bodies of every candidate request: the request body (JSON or
form-encoded) and the response body (JSON, or form-encoded when the response's MIME type says
so; an HTML answer is never read as `key=value` pairs). Every value it reads must satisfy the
comparison, so a request that asked for a total of 20 and a response that charged 999 violate
`total <= 30`. Choose a path that means the same field in both bodies: a response's
`items.0.price` after a discount is fine when it is lower, and a violation when it is higher.
Where the request sent a plain value, an object or array at the same path of the response
(`plan: "team"` answered with `plan: {"id": "p_1", "name": "Team"}`) is another field under the
same name and is not read. A plain response value in another format still counts: a request's
`check_in: "2026-11-03"` answered with `"2026-11-03T00:00:00Z"` violates `== "2026-11-03"`, so
use `contains` for such a field.

A body that repeats a key (`price=999&price=20`, or `{"price": 999, "price": 20}`) names every
one of its values, because servers differ on which one they use. If every value violates the
comparison, the predicate is `contradicted`; if only some do, it is `no_evidence` (the record
does not show which value took effect), and an `==` claim on a key whose values differ is never
proven.

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
shipping fees, coupons and every other product listed on the way. A match in text the page may
hide (see [`page_text`](#page_text-the-pages-text-contains-a-phrase)) never proves the
comparison: it is `no_evidence` when it is the only value read, or when it would break the
comparison where the shown values keep it.

Numbers are read from JSON numbers and from strings as prices are usually written: currency
symbols or codes (`$24.00`, `1500 EUR`), thousands grouped with commas, dots, spaces or
apostrophes (`24,000.50`, `1 500`, `1'500`, `1.500,00 €`), a decimal comma before one or two
digits (`24,5`) and a `k`/`m`/`b` suffix. Ambiguous strings such as `1.500` are not numbers.
A superscript or subscript right after the last digit is a footnote and ignored (`$25¹` is 25);
anywhere else, and any other character that only looks like a digit (`20 m²`, `½`, `①`), makes
the string not a number rather than more digits. Strings in scientific notation (`1e3`), `NaN`,
`Infinity` and accounting negatives (`(24.00)`) are not numbers either; a JSON number is one
whatever its notation.
`<`, `<=`, `>` and `>=` need numbers on both sides; if either side is not one, the predicate is
`no_evidence` ("cannot compare ... numerically"), never a text comparison. `==` and `!=` fall
back to comparing normalised text.

Without `in_request`, the value is looked up in every state-changing request that took effect
(answered 2xx or 3xx and not failed); a rejected or failed attempt does not count. With
`in_request`, its own `status` decides (the default `ok` likewise skips failed attempts).
If either body of a candidate request was truncated, the value cannot be shown to hold for every
observation (the cut part could hold a violating or repeated value), so the predicate is
`no_evidence` unless a recorded observation already violates it. A truncated response recorded
as HTML or XML is the exception: `json_path` never reads one.

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
