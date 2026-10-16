"""Harbour Dental: an appointment-booking site that browser-agent runs are recorded against.

A booking form with a calendar date picker, a time slot list loaded for the chosen date, field
validation in the browser and on the server, and a confirmation page with a booking reference.

Run it with ``uv run uvicorn demo.clinic.app:app --port 8001``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from html import escape
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

SERVICES: dict[str, str] = {
    "checkup": "Dental check-up",
    "hygiene": "Hygiene clean",
    "whitening": "Teeth whitening",
}
SLOTS: tuple[str, ...] = ("09:00", "10:00", "11:30", "14:00", "15:30")
MONTH: tuple[int, int] = (2026, 11)
FIRST_REFERENCE = 1001

STYLE = """
:root { --ink: #10231f; --muted: #4b5d58; --line: #d7e3df; --brand: #0f766e; --bad: #b91c1c;
  --paper: #f5faf8; }
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; color: var(--ink); background: var(--paper); }
header { display: flex; gap: 22px; padding: 14px 32px; border-bottom: 1px solid var(--line);
  background: #fff; }
header a { color: var(--muted); text-decoration: none; font-weight: 600; }
header a:first-child { color: var(--brand); margin-right: auto; }
main { max-width: 760px; margin: 0 auto; padding: 28px 32px 48px; }
label { display: block; font-weight: 600; margin: 16px 0 6px; }
input, select { font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px;
  width: 100%; max-width: 360px; background: #fff; }
button { font: inherit; padding: 9px 16px; border-radius: 8px; border: 0; background: var(--brand);
  color: #fff; font-weight: 600; cursor: pointer; }
button.secondary { background: #fff; color: var(--brand); border: 1px solid var(--brand); }
.calendar { display: none; grid-template-columns: repeat(7, 40px); gap: 4px; margin-top: 8px;
  padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: #fff;
  width: max-content; }
.calendar.open { display: grid; }
.calendar .dow { font-size: 12px; color: var(--muted); text-align: center; }
.calendar button { padding: 6px 0; background: #fff; color: var(--ink); border: 1px solid var(--line); }
.error { color: var(--bad); font-size: 14px; margin: 6px 0 0; }
.banner { padding: 12px 14px; border-radius: 10px; background: #fef2f2; color: var(--bad);
  border: 1px solid #fecaca; }
.card { padding: 20px 22px; border-radius: 14px; background: #fff; border: 1px solid var(--line); }
.reference { font-size: 26px; font-weight: 750; letter-spacing: 0.02em; }
"""


@dataclass(slots=True)
class ClinicState:
    appointments: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    next_reference: int = FIRST_REFERENCE


def _page(title: str, body: str, script: str = "") -> HTMLResponse:
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}'
        f" | Harbour Dental</title><style>{STYLE}</style></head><body>"
        '<header><a href="/">Harbour Dental</a><a href="/book">Book</a>'
        '<a href="/contact">Contact</a></header>'
        f"<main>{body}</main>{f'<script>{script}</script>' if script else ''}</body></html>"
    )
    return HTMLResponse(html)


def _long_date(value: str) -> str:
    day = dt.date.fromisoformat(value)
    return f"{day:%A} {day.day} {day:%B %Y}"


async def home(request: Request) -> Response:
    items = "".join(f"<li>{escape(name)}</li>" for name in SERVICES.values())
    return _page(
        "Home",
        "<h1>Harbour Dental</h1><p>Friendly dentistry on the waterfront. Open Monday to Friday, "
        f"9:00 to 17:00.</p><h2>Services</h2><ul>{items}</ul>"
        '<p><a id="book-link" href="/book">Book an appointment</a></p>',
    )


def _calendar() -> str:
    year, month = MONTH
    first = dt.date(year, month, 1)
    cells = [f'<span class="dow">{d}</span>' for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")]
    cells += ["<span></span>"] * first.weekday()
    day = first
    while day.month == month:
        iso = day.isoformat()
        cells.append(f'<button type="button" class="day" data-date="{iso}">{day.day}</button>')
        day += dt.timedelta(days=1)
    return f'<div class="calendar" id="calendar" aria-label="November 2026">{"".join(cells)}</div>'


BOOK_SCRIPT = """
const form = document.getElementById('booking');
const dateField = document.getElementById('date');
const time = document.getElementById('time');
document.getElementById('pick-date').onclick = () =>
  document.getElementById('calendar').classList.toggle('open');
document.querySelectorAll('.day').forEach(b => b.onclick = async () => {
  dateField.value = b.dataset.date;
  document.getElementById('calendar').classList.remove('open');
  const r = await fetch('/api/slots?date=' + b.dataset.date);
  const j = await r.json();
  time.innerHTML = j.slots.map(s => `<option value="${s}">${s}</option>`).join('');
  time.disabled = false;
});
function showErrors(errors) {
  document.querySelectorAll('.error').forEach(e => e.textContent = '');
  for (const [name, message] of Object.entries(errors)) {
    document.getElementById('error-' + name).textContent = message;
  }
  document.getElementById('banner').hidden = false;
}
form.onsubmit = async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  const missing = {};
  for (const name of ['date', 'time', 'name', 'email']) {
    if (!data[name]) missing[name] = 'This field is required.';
  }
  if (Object.keys(missing).length) { showErrors(missing); return; }
  const r = await fetch('/api/appointments', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
  const j = await r.json();
  if (r.status === 201) { location.href = '/appointments/' + j.reference; return; }
  showErrors(j.errors || {form: 'Something went wrong.'});
};
"""


async def book_page(request: Request) -> Response:
    options = "".join(f'<option value="{k}">{escape(v)}</option>' for k, v in SERVICES.items())
    body = (
        "<h1>Book an appointment</h1>"
        '<p class="banner" id="banner" hidden>Please fix the errors below.</p>'
        '<form id="booking" novalidate>'
        f'<label for="service">Service</label><select id="service" name="service">{options}</select>'
        '<label for="date">Date</label><input id="date" name="date" readonly placeholder="YYYY-MM-DD">'
        ' <button type="button" class="secondary" id="pick-date">Choose date</button>'
        f'{_calendar()}<p class="error" id="error-date"></p>'
        '<label for="time">Time</label><select id="time" name="time" disabled>'
        '<option value="">Choose a date first</option></select>'
        '<p class="error" id="error-time"></p>'
        '<label for="name">Full name</label><input id="name" name="name" autocomplete="name">'
        '<p class="error" id="error-name"></p>'
        '<label for="email">Email</label><input id="email" name="email" type="email">'
        '<p class="error" id="error-email"></p><p class="error" id="error-form"></p>'
        '<p><button type="submit" id="submit">Book appointment</button></p></form>'
    )
    return _page("Book", body, BOOK_SCRIPT)


async def appointment_page(request: Request) -> Response:
    reference = request.path_params["reference"]
    booking = _state(request).appointments.get(reference)
    if booking is None:
        return _page("Not found", "<h1>Not found</h1><p>No appointment with that reference.</p>")
    body = (
        '<div class="card"><h1>Appointment confirmed</h1>'
        f'<p>Reference <span class="reference">{escape(reference)}</span></p>'
        f"<p>{escape(SERVICES[booking['service']])} on {escape(_long_date(booking['date']))} at "
        f"{escape(booking['time'])} for {escape(booking['name'])}.</p>"
        f"<p>We sent the details to {escape(booking['email'])}.</p></div>"
    )
    return _page("Confirmed", body)


async def contact_page(request: Request) -> Response:
    return _page("Contact", "<h1>Contact</h1><p>Call 01632 960 123 or visit 4 Quay Street.</p>")


def _state(request: Request) -> ClinicState:
    return request.app.state.clinic


async def api_slots(request: Request) -> Response:
    try:
        dt.date.fromisoformat(request.query_params.get("date", ""))
    except ValueError:
        return JSONResponse({"error": "a date is required"}, status_code=400)
    return JSONResponse({"slots": list(SLOTS)})


def _validate(payload: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if payload.get("service") not in SERVICES:
        errors["form"] = "Choose a service."
    try:
        day = dt.date.fromisoformat(str(payload.get("date", "")))
    except ValueError:
        errors["date"] = "Choose a date."
    else:
        if day.weekday() >= 5:
            errors["date"] = "We are closed at weekends. Please choose a weekday."
    if payload.get("time") not in SLOTS:
        errors["time"] = "Choose an available time."
    if not str(payload.get("name", "")).strip():
        errors["name"] = "Enter your full name."
    email = str(payload.get("email", ""))
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        errors["email"] = "Enter a valid email address."
    return errors


async def api_create_appointment(request: Request) -> Response:
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"errors": {"form": "Expected a JSON object."}}, status_code=400)
    booking = cast(dict[str, Any], payload)
    errors = _validate(booking)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)
    state = _state(request)
    reference = f"HD-{state.next_reference}"
    state.next_reference += 1
    state.appointments[reference] = {
        k: str(booking[k]) for k in ("service", "date", "time", "name", "email")
    }
    return JSONResponse({"reference": reference}, status_code=201)


async def api_reset(request: Request) -> Response:
    request.app.state.clinic = ClinicState()
    return JSONResponse({"ok": True})


def create_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/", home),
            Route("/book", book_page),
            Route("/contact", contact_page),
            Route("/appointments/{reference}", appointment_page),
            Route("/api/slots", api_slots, methods=["GET"]),
            Route("/api/appointments", api_create_appointment, methods=["POST"]),
            Route("/api/reset", api_reset, methods=["POST"]),
        ]
    )
    app.state.clinic = ClinicState()
    return app


app = create_app()
