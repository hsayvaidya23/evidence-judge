"""Evidence the agent made rather than the site: code it ran in the page, responses its own route
handler supplied, and links on pages it wrote itself.

An agent that runs `page.evaluate("document.querySelector('#stock').textContent = 'In stock'")`
or answers the shop's API with `route.fulfill` records a page or a response that looks like the
site's. Ingesters record the first as an INJECT action and the second as a `synthetic` network
event; from the first of them on (`tainted_at`), no page state, response or request is the
site's alone, so `untampered` cuts the evidence there and `tampering` says why. The trace
ingester also records as INJECT every call it does not know to be harmless (deny by default:
storage and cookie writes, CDP sessions, WebSocket routes, ...).

Opening a `javascript:` or `vbscript:` URL runs its code in the page the way `evaluate` does;
ingesters record that navigation as an INJECT (`is_script_url`). So is opening any URL that
shows something other than a site's page under that page's URL (`opens_unserved_page`): a
`view-source:` page lists a site page's raw markup, scripts and comments, and Chromium reports
it under the http URL of the page whose source it shows, so its text would read as that page's.

A file the agent uploads by path, folder or stream (or a binary one), or an upload the
recording names no file for, is not in the recording, and the page may show its contents: page text after such an upload (`unseen_uploads`) may be the
agent's. `untampered` also drops page states, screenshots and response bodies from the first of
them on, and `doubt` names it. Unlike an injection it does not stop the run from passing: the
requests the browser sent and their statuses are still the site's, and a predicate that holds on
them still proves.

A script the spec's author vouched for is not the agent writing the page: `trust_scripts`
records an INJECT whose `script_sha256` the spec's `trusted_scripts` lists as OTHER.

A page the agent opened that no site served (data:, blob:, about:blank ...) holds
whatever the agent wrote into it, links included, so following one of its links is the agent
choosing the URL. `mark_agent_page_navigations` records such a navigation as a GOTO of its
target, which makes the target's URL agent input like any URL the agent typed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Final

from evidence_judge.core.actions import GOTO, INJECT, OTHER, UPLOAD, canonical_action
from evidence_judge.core.evidence import ActionEvent, NetworkEvent, Run, is_web_page_url

MAX_NAMED: Final = 2
"""Injected calls and synthetic requests named in `tampering`'s reason."""
NAVIGATION_SUFFIX: Final = ".goto"
"""Suffix of the id of a GOTO that `mark_agent_page_navigations` adds after an action."""
SCRIPT_SCHEMES: Final = ("javascript:", "vbscript:")
"""URL schemes whose navigation runs the URL's code in the current page."""
NAVIGABLE_SCHEMES: Final = frozenset({"http", "https", "data", "blob"})
"""Schemes an agent may open without tainting the run: a site's pages, and pages the agent wrote
(data:, blob:) that Chromium reports under their own URL, which never counts as a site's
(`is_agent_page`)."""
_URL_SPACE = re.compile(r"[\t\n\r]")
_URL_LEADING = "".join(chr(c) for c in range(0x21))
_SCHEME = re.compile(r"([a-z][a-z0-9+.\-]*):(.*)", re.DOTALL)
_PORT = re.compile(r"\d+(?:[/?#]|$)")


def is_script_url(url: str | None) -> bool:
    """`url` runs code in the page when opened (SCRIPT_SCHEMES) instead of loading a page. Its
    scheme is read the way a browser reads it: leading spaces and control characters dropped,
    tabs and newlines anywhere ignored, in any case (`" Java\tScript:alert(1)"`)."""
    if not url:
        return False
    return _URL_SPACE.sub("", url).lstrip(_URL_LEADING).lower().startswith(SCRIPT_SCHEMES)


def opens_unserved_page(url: str | None) -> bool:
    """Opening `url` shows something other than a site's page or a page reported under its own
    URL: a script URL (`is_script_url`), or any scheme but NAVIGABLE_SCHEMES (view-source:,
    file:, chrome:, about: other than exactly about:blank, ...). The scheme is read as
    `is_script_url` reads it; a URL without one ("shop.test/cart") or that starts with a host
    and port ("localhost:8000/") is a site's page."""
    if not url:
        return False
    text = _URL_SPACE.sub("", url).strip(_URL_LEADING).lower()
    match = _SCHEME.match(text)
    if match is None or _PORT.match(match.group(2)) or text == "about:blank":
        return False
    return match.group(1) not in NAVIGABLE_SCHEMES


def script_hash(api: str, code: str, arg: object = None) -> str:
    """The hash a task spec's `trusted_scripts` lists to exempt one injected script: SHA-256,
    in hex, of the UTF-8 text `api`, a newline, `code`, a newline and `arg` as canonical JSON
    (keys sorted, no spaces, non-ASCII kept). `api` is the call as the INJECT action names it
    (evaluate, evaluateHandle, $eval, $$eval, waitForFunction, addInitScript, addScriptTag,
    addStyleTag, setContent, and goto for a `javascript:` URL), `code` its code (the whole URL
    for goto) exactly as recorded and `arg` its argument in Playwright's wire form as the trace
    records it (`{"n": 500}` for 500), None for none."""
    canonical = json.dumps(arg, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{api}\n{code}\n{canonical}".encode()).hexdigest()


def trust_scripts(run: Run, trusted: Iterable[str]) -> Run:
    """`run` with every INJECT action whose `script_sha256` is in `trusted` recorded as OTHER:
    code the spec's author vouched for is not the agent writing the page. Unchanged when none
    is."""
    hashes = {h.lower() for h in trusted}
    if not hashes or not any(a.script_sha256 in hashes for a in injections(run)):
        return run
    return run.model_copy(
        update={
            "actions": [
                a.model_copy(update={"name": OTHER})
                if a.script_sha256 in hashes and canonical_action(a.name) == INJECT
                else a
                for a in run.actions
            ]
        }
    )


def injections(run: Run) -> list[ActionEvent]:
    """The actions that ran the agent's code in the page or replaced its content."""
    return [a for a in run.actions if canonical_action(a.name) == INJECT]


def tainted_at(run: Run) -> float | None:
    """When the first injection ran or the first synthetic request started, or None."""
    times = [a.ts for a in injections(run)] + [n.ts for n in run.network if n.synthetic]
    return min(times, default=None)


def tampering(run: Run) -> str | None:
    """Why evidence from `tainted_at` on is not the site's, naming the calls, or None."""
    parts: list[str] = []
    code = injections(run)
    if code:
        named = ", ".join(_named(a) for a in code[:MAX_NAMED])
        more = f" and {len(code) - MAX_NAMED} more" if len(code) > MAX_NAMED else ""
        parts.append(
            f"the agent ran its own code in the page or tampered with the browser: {named}{more}"
        )
    mocked = [n for n in run.network if n.synthetic]
    if mocked:
        named = ", ".join(f"{n.method} {n.url} ({n.id})" for n in mocked[:MAX_NAMED])
        more = f" and {len(mocked) - MAX_NAMED} more" if len(mocked) > MAX_NAMED else ""
        parts.append(
            f"the agent's route handler answered or rewrote requests (route.fulfill or "
            f"route.continue with overrides): {named}{more}"
        )
    if not parts:
        return None
    return "; ".join(parts) + "; evidence from then on is not the site's"


def _named(action: ActionEvent) -> str:
    sha = f", sha256 {action.script_sha256}" if action.script_sha256 else ""
    return f"{action.value or action.name} ({action.id}{sha})"


def unseen_uploads(run: Run) -> list[ActionEvent]:
    """The uploads whose files' text the recording does not hold (`file_text`): sent by path,
    folder or stream, binary, or not named at all. Every upload without file text counts."""
    return [a for a in run.actions if canonical_action(a.name) == UPLOAD and not a.file_text]


def unseen_upload(run: Run) -> str | None:
    """Why page text from the first `unseen_uploads` on may be the agent's, naming the files,
    or None."""
    unseen = unseen_uploads(run)
    if not unseen:
        return None
    named = ", ".join(f"{a.value or 'a file'} ({a.id})" for a in unseen[:MAX_NAMED])
    more = f" and {len(unseen) - MAX_NAMED} more" if len(unseen) > MAX_NAMED else ""
    return (
        f"the agent uploaded {named}{more} whose contents are not in the recording; page text "
        "and response bodies from then on may show them"
    )


def doubt(run: Run) -> str | None:
    """Why some evidence is not the site's (`tampering`, `unseen_upload`), or None: what
    `untampered` drops."""
    parts = [p for p in (tampering(run), unseen_upload(run)) if p is not None]
    return "; ".join(parts) or None


def untampered(run: Run) -> Run:
    """`run` as the site alone made it: page states, screenshots and requests from `tainted_at`
    on are dropped, synthetic requests with them, and later actions lose their page URL (it is a
    page the agent may have written). The actions themselves are kept: they are what the agent
    did. From the first `unseen_uploads` on, page states, screenshots and page URLs go the same
    way and requests lose their response bodies. Unchanged when there is neither."""
    cut = tainted_at(run)
    if cut is not None:
        run = _cut(run, cut, network=[n for n in run.network if n.ts < cut and not n.synthetic])
    unseen = [a.ts for a in unseen_uploads(run)]
    if unseen:
        at = min(unseen)
        network = [
            n
            if n.ts < at or n.response_body is None
            else n.model_copy(update={"response_body": None})
            for n in run.network
        ]
        run = _cut(run, at, network=network)
    return run


def _cut(run: Run, cut: float, *, network: list[NetworkEvent]) -> Run:
    snapshots = [s for s in run.snapshots if s.ts < cut]
    kept = {s.id for s in snapshots}
    initial = run.initial_state
    return run.model_copy(
        update={
            "actions": [
                a if a.ts < cut or a.url is None else a.model_copy(update={"url": None})
                for a in run.actions
            ],
            "network": network,
            "snapshots": snapshots,
            "screenshots": [s for s in run.screenshots if s.ts < cut],
            "initial_state": initial
            if initial is not None and initial.snapshot_id in kept
            else None,
            "final_state": None,
        }
    )


def is_agent_page(url: str | None) -> bool:
    """A page no site served (not http or https), so the agent decided what it shows."""
    return url is not None and not is_web_page_url(url)


def mark_agent_page_navigations(run: Run) -> Run:
    """`run` with a GOTO before every action taken on a page the agent made (`is_agent_page`)
    that was followed by a web page: the first http or https URL recorded after it (a page
    state, a document load or a later action's page), else the final page. The GOTO stands for
    that action as the navigation it started: it has the action's time, its page URL, the
    target as its value, and the action's id with NAVIGATION_SUFFIX. GOTO and INJECT actions get none: a GOTO already names its URL, and what
    follows an injection is not the site's anyway. Idempotent."""
    marked = {a.id for a in run.actions if a.id.endswith(NAVIGATION_SUFFIX)}
    actions: list[ActionEvent] = []
    for action in run.actions:
        target = (
            None
            if not is_agent_page(action.url)
            or canonical_action(action.name) in (GOTO, INJECT)
            or action.id.endswith(NAVIGATION_SUFFIX)
            or action.id + NAVIGATION_SUFFIX in marked
            else _next_web_url(run, action.ts)
        )
        if target is not None:
            actions.append(
                ActionEvent(
                    id=action.id + NAVIGATION_SUFFIX,
                    ts=action.ts,
                    name=GOTO,
                    value=target,
                    url=action.url,
                )
            )
        actions.append(action)
    added = len(actions) > len(run.actions)
    return run.model_copy(update={"actions": actions}) if added else run


def _next_web_url(run: Run, after: float) -> str | None:
    later = [
        *((s.ts, s.url) for s in run.snapshots),
        *(
            (n.ts, n.url)
            for n in run.network
            if n.method.upper() == "GET" and (n.resource_type or "").lower() == "document"
        ),
        *((a.ts, a.url) for a in run.actions if a.url),
    ]
    found = [(ts, url) for ts, url in later if ts > after and url and is_web_page_url(url)]
    if found:
        return min(found, key=lambda pair: pair[0])[1]
    final = run.final_state
    return final.url if final is not None and is_web_page_url(final.url) else None
