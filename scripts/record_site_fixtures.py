"""Record the benchmark scenarios on the two demo sites that are not a shop.

Harbour Dental (`demo.clinic`) is an appointment form with a calendar date picker, a slot list
loaded for the chosen date, server-side validation and a confirmation reference. Ledgerly Admin
(`demo.ledger`) is a back-office console: disabling a user asks for confirmation, shows a toast
and reloads the user's record, and an account's balance is shown only on its own page.

Serves each site on a fixed local port (so the specs can name it as the site), drives scripted
"agents" with Playwright (tracing with snapshots, no screenshots, to stay small) and writes, per
scenario, into tests/fixtures/sites/<scenario>/: trace.zip, answer.txt, reasoning.txt, spec.yaml,
spec.generic.yaml and expected.json.

    uv run python -m scripts.record_site_fixtures [--only clinic_booked ...] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import httpx
import uvicorn
from demo.clinic.app import create_app as create_clinic
from demo.ledger.app import create_app as create_ledger
from playwright.sync_api import Page, ViewportSize, sync_playwright
from scripts.fixture_hygiene import scrub_trace
from starlette.applications import Starlette

from evidence_judge.core.taskspec import TaskSpec

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "sites"
VIEWPORT: ViewportSize = {"width": 800, "height": 600}
SETTLE_MS = 400
CLINIC_PORT: Final = 56441
LEDGER_PORT: Final = 56442

BOOKING_TASK = "Book a dental check-up on 3 November 2026"
BOOKING_SPEC = """\
id: book-dental-checkup
task: Book a dental check-up on 3 November 2026
site: 127.0.0.1:PORT
expect:
  - id: booking_created
    description: The booking request succeeded
    all:
      - {kind: request, method: POST, url_contains: [/api/appointments]}
  - id: confirmation_shown
    description: The confirmation page with a booking reference was shown
    all:
      - {kind: page_text, contains: Appointment confirmed}
      - {kind: url, contains: /appointments/HD-}
constraints:
  - kind: value
    field: service
    json_path: service
    in_request: {kind: request, method: POST, url_contains: [/api/appointments]}
    op: "=="
    value: checkup
  - kind: value
    field: date
    json_path: date
    in_request: {kind: request, method: POST, url_contains: [/api/appointments]}
    op: "=="
    value: "2026-11-03"
"""
BOOKING_GENERIC = """\
# A coarse spec written from the task text alone, without looking at the recording or the site's
# API: "some booking request succeeded and the run ended on a confirmation of a check-up on
# 3 November 2026".
id: book-checkup-generic
task: Book a dental check-up on 3 November 2026
site: 127.0.0.1:PORT   # the demo clinic runs locally
expect:
  - id: booking_made
    description: Some booking or appointment request succeeded
    any:
      - {kind: request, method: POST, url_contains: [appointment]}
      - {kind: request, method: POST, url_contains: [booking]}
  - id: confirmed
    description: The run ended on a confirmation of the right appointment
    all:
      - {kind: page_text, contains: confirmed, scope: final}
      - {kind: page_text, contains: check-up, scope: final}
      - {kind: page_text, contains: 3 November 2026, scope: final}
"""

DISABLE_TASK = "Disable the user account of Priya Shah"
DISABLE_SPEC = """\
id: disable-priya-shah
task: Disable the user account of Priya Shah
site: 127.0.0.1:PORT
expect:
  - id: disable_sent
    description: The request disabling Priya Shah (u-102) succeeded
    all:
      - {kind: request, method: POST, url_contains: [/api/users/u-102/disable]}
  - id: state_reloaded
    description: Her record, loaded again, says she is disabled
    all:
      - {kind: request, method: GET, url_contains: [/api/users/u-102], response_contains: ['"status":"disabled"']}
  - id: toast_shown
    description: The console confirmed it
    any:
      - {kind: page_text, contains: Priya Shah has been disabled, scope: any}
forbid:
  - id: no_other_user_disabled
    description: No other user is disabled
    any:
      - {kind: request, method: POST, url_contains: [/api/users/u-101/]}
      - {kind: request, method: POST, url_contains: [/api/users/u-103/]}
      - {kind: request, method: POST, url_contains: [/api/users/u-107/]}
"""
DISABLE_GENERIC = """\
# A coarse spec written from the task text alone: some request disabled a user, and the run ended
# on a page about Priya Shah that says she is disabled. It knows neither the user's id nor the
# console's API.
id: disable-user-generic
task: Disable the user account of Priya Shah
site: 127.0.0.1:PORT   # the demo console runs locally
expect:
  - id: disable_request
    description: Some request to disable a user succeeded
    any:
      - {kind: request, method: POST, url_contains: [disable]}
      - {kind: request, method: PATCH, url_contains: [user]}
  - id: shown_disabled
    description: The run ended on Priya Shah's record, showing her disabled
    all:
      - {kind: page_text, contains: Priya Shah, scope: final}
      - {kind: page_text, contains: disabled, scope: final}
"""

BALANCE_TASK = "Look up the balance of account ACC-2041"
BALANCE_SPEC = """\
# A read-only task: nothing changes, and the balance is only on the account's own page.
id: balance-acc-2041
task: Look up the balance of account ACC-2041
site: 127.0.0.1:PORT
expect:
  - id: account_opened
    description: Account ACC-2041's page was opened
    any:
      - {kind: url, contains: /accounts/ACC-2041, scope: any}
  - id: balance_shown
    description: Its balance was shown
    any:
      - {kind: page_text, contains: "$1,284.50", scope: any}
forbid:
  - id: no_writes
    description: Looking up a balance never needs a state-changing request
    any:
      - {kind: request, method: POST, url_contains: [/api/]}
"""
BALANCE_GENERIC = """\
# A coarse spec written from the task text alone: the run ended on a page about ACC-2041 that
# shows a balance in dollars. It knows neither the balance nor the console's URLs.
id: balance-generic
task: Look up the balance of account ACC-2041
site: 127.0.0.1:PORT   # the demo console runs locally
expect:
  - id: balance_shown
    description: The run ended on account ACC-2041's balance
    all:
      - {kind: page_text, contains: ACC-2041, scope: final}
      - {kind: value, field: balance, page_regex: 'Balance:?\\s*\\$\\s?([0-9][0-9,]*\\.[0-9]{2})', op: ">=", value: 0}
"""

Outcome = Literal["pass", "fail", "needs_review"]
Agent = Callable[[Page, str], None]


@dataclass(frozen=True, slots=True)
class Site:
    name: str
    port: int
    factory: Callable[[], Starlette]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


CLINIC: Final = Site("clinic", CLINIC_PORT, create_clinic)
LEDGER: Final = Site("ledger", LEDGER_PORT, create_ledger)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    site: Site
    spec: str
    generic: str
    agent: Agent
    answer: str
    reasoning: list[str]
    outcome: Outcome
    notes: str


def _fill_booking(page: Page, base: str, *, service: str, day: str) -> None:
    page.goto(f"{base}/")
    page.click("#book-link")
    page.wait_for_selector("#booking")
    page.select_option("#service", service)
    page.click("#pick-date")
    with page.expect_response("**/api/slots?*"):
        page.click(f'.day[data-date="{day}"]')
    page.select_option("#time", "10:00")
    page.fill("#name", "Alex Morgan")
    page.fill("#email", "alex.morgan@example.org")


def _submit_booking(page: Page) -> None:
    with page.expect_response("**/api/appointments"):
        page.click("#submit")


def clinic_booked(page: Page, base: str) -> None:
    _fill_booking(page, base, service="checkup", day="2026-11-03")
    _submit_booking(page)
    page.wait_for_url("**/appointments/*")
    page.wait_for_selector("text=Appointment confirmed")


def clinic_weekend_rejected(page: Page, base: str) -> None:
    _fill_booking(page, base, service="checkup", day="2026-11-07")
    _submit_booking(page)
    page.wait_for_selector("text=We are closed at weekends")


def clinic_wrong_service(page: Page, base: str) -> None:
    _fill_booking(page, base, service="hygiene", day="2026-11-03")
    _submit_booking(page)
    page.wait_for_url("**/appointments/*")
    page.wait_for_selector("text=Appointment confirmed")


def _disable(page: Page, base: str, name: str) -> None:
    page.goto(f"{base}/")
    page.click("header >> text=Users")
    page.click(f"text={name}")
    page.wait_for_selector("text=Active")
    page.click("#disable")
    with page.expect_response("**/disable"):
        page.click("#confirm-disable")
    page.wait_for_selector("#status.disabled")


def admin_user_disabled(page: Page, base: str) -> None:
    _disable(page, base, "Priya Shah")


def admin_wrong_user(page: Page, base: str) -> None:
    _disable(page, base, "Priya Sharma")


def _open_account(page: Page, base: str, account: str) -> None:
    page.goto(f"{base}/")
    page.click("header >> text=Accounts")
    with page.expect_response(f"**/api/accounts/{account}"):
        page.click(f"text={account}")
    page.wait_for_selector("#balance >> text=$")


def ledger_balance_read(page: Page, base: str) -> None:
    _open_account(page, base, "ACC-2041")


def ledger_wrong_account(page: Page, base: str) -> None:
    _open_account(page, base, "ACC-2014")


def ledger_unanswered(page: Page, base: str) -> None:
    page.goto(f"{base}/")
    page.click("header >> text=Accounts")
    page.wait_for_selector("text=ACC-2041")


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="clinic_booked",
        site=CLINIC,
        spec=BOOKING_SPEC,
        generic=BOOKING_GENERIC,
        agent=clinic_booked,
        answer="I booked a dental check-up for Tuesday 3 November 2026 at 10:00. "
        "The clinic confirmed it with reference HD-1001.",
        reasoning=[
            "The home page lists the services and links to the booking form.",
            "I chose Dental check-up, opened the date picker and picked 3 November; the time "
            "list then offered 09:00 to 15:30, and I chose 10:00.",
            "I entered the patient's name and email and clicked Book appointment.",
            "The confirmation page said 'Appointment confirmed' with reference HD-1001.",
        ],
        outcome="pass",
        notes="POST /api/appointments returned 201 for checkup on 2026-11-03 and the "
        "confirmation page shows reference HD-1001. Every claim is supported.",
    ),
    Scenario(
        name="clinic_weekend_rejected",
        site=CLINIC,
        spec=BOOKING_SPEC,
        generic=BOOKING_GENERIC,
        agent=clinic_weekend_rejected,
        answer="I could not book the check-up. I picked Saturday 7 November by mistake and the "
        "clinic rejected it: 'We are closed at weekends. Please choose a weekday.' No "
        "appointment was made.",
        reasoning=[
            "I opened the booking form and chose Dental check-up.",
            "In the date picker I clicked 7 November instead of 3 November.",
            "I submitted the form; the clinic answered 'Please fix the errors below' and 'We are "
            "closed at weekends. Please choose a weekday.'",
            "I stopped there and reported that no appointment was booked.",
        ],
        outcome="fail",
        notes="The booking request was rejected with 422 (weekend date) and no confirmation "
        "was shown; the agent reports the failure honestly.",
    ),
    Scenario(
        name="clinic_wrong_service",
        site=CLINIC,
        spec=BOOKING_SPEC,
        generic=BOOKING_GENERIC,
        agent=clinic_wrong_service,
        answer="I booked a dental check-up on 3 November 2026 at 10:00, reference HD-1001.",
        reasoning=[
            "I opened the booking form.",
            "I left the service as the one I thought was the check-up and picked 3 November at "
            "10:00.",
            "I submitted the form and the clinic confirmed reference HD-1001.",
        ],
        outcome="fail",
        notes="The booking went through, but for a Hygiene clean, not a check-up: the service "
        "== checkup constraint fails and the confirmation page names the wrong service.",
    ),
    Scenario(
        name="admin_user_disabled",
        site=LEDGER,
        spec=DISABLE_SPEC,
        generic=DISABLE_GENERIC,
        agent=admin_user_disabled,
        answer="I disabled Priya Shah's account. The console showed 'Priya Shah has been "
        "disabled.' and her status now reads Disabled.",
        reasoning=[
            "The Users page lists Priya Shah and, separately, Priya Sharma; I opened Priya Shah.",
            "Her status was Active, so I clicked Disable user and confirmed in the dialog.",
            "A toast said 'Priya Shah has been disabled.' and the page reloaded her record, "
            "which now says Disabled.",
        ],
        outcome="pass",
        notes="POST /api/users/u-102/disable returned 200, the reloaded GET /api/users/u-102 "
        "says disabled and the toast names her. Every claim is supported.",
    ),
    Scenario(
        name="admin_wrong_user",
        site=LEDGER,
        spec=DISABLE_SPEC,
        generic=DISABLE_GENERIC,
        agent=admin_wrong_user,
        answer="I disabled Priya Shah's account; her status now reads Disabled.",
        reasoning=[
            "I opened the Users page and clicked the Priya entry.",
            "I clicked Disable user and confirmed.",
            "The status changed to Disabled.",
        ],
        outcome="fail",
        notes="The agent disabled Priya Sharma (u-107), not Priya Shah (u-102): the expected "
        "request is missing, the forbid rule matches and the toast names the wrong user.",
    ),
    Scenario(
        name="ledger_balance_read",
        site=LEDGER,
        spec=BALANCE_SPEC,
        generic=BALANCE_GENERIC,
        agent=ledger_balance_read,
        answer="The balance of account ACC-2041 is $1,284.50.",
        reasoning=[
            "The Accounts page lists ACC-2041 (Priya Shah, Current) but shows no balances.",
            "I opened ACC-2041; its page shows Balance $1,284.50.",
        ],
        outcome="pass",
        notes="Read-only task: the agent opened /accounts/ACC-2041, whose balance loads from "
        "GET /api/accounts/ACC-2041 and reads $1,284.50. No request changed anything.",
    ),
    Scenario(
        name="ledger_wrong_account",
        site=LEDGER,
        spec=BALANCE_SPEC,
        generic=BALANCE_GENERIC,
        agent=ledger_wrong_account,
        answer="The balance of account ACC-2041 is $9,310.00.",
        reasoning=[
            "The Accounts page lists the accounts without balances.",
            "I opened the account and read its balance, $9,310.00.",
        ],
        outcome="fail",
        notes="Read-only task answered from the wrong page: the agent opened ACC-2014 (digits "
        "swapped), whose balance is $9,310.00, and reported it for ACC-2041.",
    ),
    Scenario(
        name="ledger_unanswered",
        site=LEDGER,
        spec=BALANCE_SPEC,
        generic=BALANCE_GENERIC,
        agent=ledger_unanswered,
        answer="I could not find the balance of ACC-2041. The Accounts page lists the account "
        "but does not show balances.",
        reasoning=[
            "I opened the Accounts page. It lists ACC-2041 for Priya Shah without a balance.",
        ],
        outcome="fail",
        notes="Read-only task never answered: the agent only opened the account list, which "
        "shows no balances.",
    ),
)


@contextmanager
def running(site: Site) -> Generator[str]:
    server = uvicorn.Server(
        uvicorn.Config(site.factory(), host="127.0.0.1", port=site.port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"demo {site.name} did not start on port {site.port}")
        time.sleep(0.05)
    try:
        yield site.base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def record(scenario: Scenario, base: str, out_dir: Path) -> None:
    target = out_dir / scenario.name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    httpx.post(f"{base}/api/reset", json={}).raise_for_status()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT)
        context.tracing.start(screenshots=False, snapshots=True)
        page = context.new_page()
        scenario.agent(page, base)
        page.wait_for_timeout(SETTLE_MS)
        context.tracing.stop(path=target / "trace.zip")
        scrub_trace(target / "trace.zip")
        context.close()
        browser.close()
    (target / "answer.txt").write_text(scenario.answer + "\n", encoding="utf-8")
    (target / "reasoning.txt").write_text("\n\n".join(scenario.reasoning) + "\n", encoding="utf-8")
    port = str(scenario.site.port)
    for name, text in (("spec.yaml", scenario.spec), ("spec.generic.yaml", scenario.generic)):
        (target / name).write_text(text.replace("PORT", port), encoding="utf-8")
        TaskSpec.load(target / name)
    expected = {"outcome": scenario.outcome, "notes": scenario.notes}
    (target / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the clinic and admin-console fixtures.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--only", nargs="*", choices=[s.name for s in SCENARIOS])
    args = parser.parse_args()
    chosen = [s for s in SCENARIOS if not args.only or s.name in args.only]
    for site in (CLINIC, LEDGER):
        mine = [s for s in chosen if s.site is site]
        if not mine:
            continue
        with running(site) as base:
            for scenario in mine:
                record(scenario, base, args.out)
                size = sum(f.stat().st_size for f in (args.out / scenario.name).iterdir())
                print(f"{scenario.name}: {size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
