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
| `snapshots` | DOM snapshots: URL, title, visible text, HTML | `snap#N` |
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
requests it did not see. A HAR next to a browser-use history adds the `network` channel the
history lacks.

Check what a recording holds with `evidence-judge inspect` before judging it.

### Page text and the final page

Snapshot and final-state text is what the page itself shows. Text typed into `<input>` and
`<textarea>` fields (and their default values) is left out, because the agent controls it: a
search box holding "Order #1042 confirmed" is not the page confirming the order. Button labels
and the selected `<option>` are kept. The number of values left out is in the trace's
`metadata["form_values_excluded"]`.

`final_state` comes from the **main page**: the tab of the agent's last action, or the first tab
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
