---
description: The evidence channels Evidence Judge reads (actions, network, snapshots, screenshots, console, final state), which recording captures what, and the absence rule.
---

# Evidence channels

Every recording is normalised into one `Run`. Its events get stable ids, and `Run.captured` lists
the **channels** the source actually recorded.

| Channel | Events | Ids |
| --- | --- | --- |
| `actions` | Clicks, fills, navigations, key presses, selects, uploads | `act#N` |
| `network` | Requests and responses: method, URL, status, headers, bodies (truncated at `EVJ_MAX_BODY_CHARS`) | `net#N` |
| `snapshots` | DOM snapshots: URL, title, page text | `snap#N` |
| `screenshots` | Screenshot images, read lazily | `shot#N` |
| `console` | Console messages | `con#N` |
| `final_state` | The page at the end of the run: URL, title, text | `final` |

## What each source captures

| Source | Flag | Channels |
| --- | --- | --- |
| Playwright trace (v7–v10) | `--trace` | `actions` and `console`, plus `network`, `snapshots`, `screenshots` and `final_state` when the trace contains them |
| HAR 1.2 | `--har` | `network` |
| browser-use history | `--browser-use` | `actions`, plus `screenshots` when the history has them |

Sources merge. A HAR recorded with a trace fills in bodies and statuses the trace missed and adds
requests it did not see; those requests are marked `har_only` and the filled fields `har_filled`.
What only the HAR holds can contradict a claim and show a side effect but never prove anything:
a HAR is a file anyone can write. A HAR next to a browser-use history adds the `network` channel the
history lacks.

Check what a recording holds with `evidence-judge inspect` before judging it.

Only a trace and a browser-use history show whether the agent made evidence itself: a trace
records code it ran in the page (`evaluate`, `add_init_script`, `set_content`, ...) as `inject`
actions and marks responses its route handler supplied (`route.fulfill`) or requests it rewrote
(`route.continue` with overrides) as `synthetic`; a browser-use history records its `evaluate`
action, every custom tool (any action that is not one of browser-use's built-in tools) and any
action that carries code (`code`, `script`, `js`, `javascript` or `expression`) as `inject`. A HAR alone shows neither, so a HAR-only run cannot reveal it. See
[evidence that proves nothing](claims-and-predicates.md#evidence-that-proves-nothing).

Every verdict states this as `tamper_detection`, with a `level` and a `reason`, and adds a
warning when the level is not `full`:

| Level | Source | What it can show |
| --- | --- | --- |
| `full` | A Playwright trace (with or without a history, or a HAR that adds nothing to it) | Every call the agent made through Playwright on the traced browser context: scripts, route handlers, storage and cookie writes, CDP sessions. Not a second context or an `APIRequestContext` it opened itself |
| `partial` | A Playwright trace merged with a HAR that added requests or filled fields | As for `full`; the reason counts what the HAR added, which proves nothing |
| `partial` | A browser-use history (without a trace) | The model's actions, including its `evaluate` and custom tools; not what ran beneath them (CDP calls, request changes) |
| `none` | A HAR alone, or any source without a call log | Nothing: a forged HAR looks the same as a recorded one |

No source shows state that existed before recording started (a persistent profile, cookies,
storage, service workers), and each is only as honest as whatever produced it. For CI gating,
record a Playwright trace from the harness, not the agent, and pass
`--require-tamper-evidence` (or set `require_tamper_evidence` in the spec) so only a `full`
run can pass: a trace whose HAR added anything is `partial`, so judge it without the HAR. See [trust assumptions](../../SECURITY.md#trust-assumptions).

### Page text and the final page

Snapshot and final-state text (`text`) is the text the page shows. Text the snapshot shows
hidden is left out: the `hidden` attribute and a closed `<dialog>` unless the page's styles
show them, a closed `<details>` body (its `<summary>` stays), an SVG `<title>` or `<desc>`,
light-DOM children no `<slot>` shows, and what the cascade certainly sets to `display: none`,
`visibility: hidden` or `content-visibility: hidden` (inline styles, `<style>` elements,
constructed stylesheets, and the linked and `@import`ed stylesheets the trace holds; each shadow
root is its own scope). Text whose visibility depends on something the judge does not fully
model (a condition or selector it cannot evaluate, `var()`, near-zero opacity or font size, a
transparent colour, clipping, off-screen offsets, a utility class no recorded sheet defines, or
a stylesheet the trace does not hold; one the trace records as failed, with an error status or
without a response, applied nothing and counts as empty) goes to `unsure_text` instead: it never proves a
predicate and keeps its absence from contradicting one. `opaque` marks a page with an empty
custom element, which may render a closed shadow root no snapshot holds, or with a shown element
a `::before` or `::after` rule gives text (`content` with a letter or digit, `attr()` or a
counter), or with a shown `<iframe>` or `<frame>` that loads a document (`src` or `srcdoc`):
the reader does not read frames, so their text never proves anything either way. See
[`page_text`](../task-specs.md#page_text-the-pages-text-contains-a-phrase).
Text typed into `<input>` and
`<textarea>` fields (and their default values) is left out, because the agent controls it: a
search box holding "Order #1042 confirmed" is not the page confirming the order. So is the
text of an editable (`contenteditable`) element and the selected `<option>` of every
`<select>`: the agent picks the option, so choosing "In stock" in a filter is not the page
saying the product is in stock. Button labels are kept. The value of a `readonly` or `disabled`
field, which the agent cannot type into, goes to `unsure_text` as well: a site that shows a
status in one is not contradicted, but the value never proves anything. The number of values left out is in the
trace's `metadata["form_values_excluded"]`.

`final_state` comes from the **main page**: the tab of the agent's last action (other than code it
ran in a page), or the first tab
opened when no action can be placed on one. Tabs opened after the last action (popups, ads) do
not count, and neither do `about:blank`, `data:`, `blob:`, `devtools:` or other browser-internal
pages: the final state is the last snapshot of the main page that a site served, with that tab's
last screenshot. A recording that never left such pages has no `final_state`. browser-use
histories follow the same URL rule.

## The absence rule

> **Absence counts only on captured channels.**

"The agent never submitted the form" is a contradiction only if the network was recorded. If it
was not, a missing request proves nothing: the claim is `no_evidence`, and a spec expectation that
needs the network is `met: null`. A key claim left without evidence sends the run to
`NEEDS_REVIEW`; it can never pass on a missing channel, and it can never fail on one either.

When a core channel (`network`, `snapshots` or `final_state`) is missing, the verdict carries a
warning:

```text
evidence not captured: network; claims needing it cannot be contradicted, only left unverified
```

### Strong and weak absence

Not every absence is equally convincing:

| Evidence | Absence means | Confidence |
| --- | --- | --- |
| No matching request in a captured network log | contradicted | full |
| Text missing from the final page | contradicted: the final state is authoritative | full |
| URL never visited, with document navigations captured in the network log | contradicted | full |
| Text or URL (`scope: any`) missing from every snapshot, with no navigations captured | contradicted, **weak** (0.6): snapshots are point-in-time and an SPA can change between them | review only |
| URL never visited, with only actions captured | `no_evidence` | — |
| Request body phrase missing from a **truncated** body | `no_evidence`: the rest was not recorded | — |

Weak contradictions never fail a run on their own, however many there are. See
[Outcomes & rules](outcomes.md).

## Telemetry is not a side effect

Pings, beacons and CSP reports are skipped when looking for unexplained state changes. The exact
rule is in [Architecture](../architecture.md#side-effects-and-telemetry).
