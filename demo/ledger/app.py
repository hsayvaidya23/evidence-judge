"""Ledgerly Admin: a back-office console that browser-agent runs are recorded against.

User pages load their record from a JSON API; disabling a user asks for confirmation, shows a
toast and reloads the record. Account pages load the balance the same way, and the account list
shows no balances, so a balance can only be read on the account's own page.

Run it with ``uv run uvicorn demo.ledger.app:app --port 8002``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    owner: str
    kind: str
    balance: str


USERS: dict[str, tuple[str, str]] = {
    "u-101": ("Jordan Lee", "jordan.lee@example.com"),
    "u-102": ("Priya Shah", "priya.shah@example.com"),
    "u-103": ("Sam Okafor", "sam.okafor@example.com"),
    "u-107": ("Priya Sharma", "priya.sharma@example.com"),
}
ACCOUNTS: dict[str, Account] = {
    a.id: a
    for a in (
        Account("ACC-2014", "Jordan Lee", "Savings", "9,310.00"),
        Account("ACC-2041", "Priya Shah", "Current", "1,284.50"),
        Account("ACC-2105", "Sam Okafor", "Current", "56.12"),
    )
}

STYLE = """
:root { --ink: #111827; --muted: #4b5563; --line: #e5e7eb; --brand: #4338ca; --ok: #047857;
  --bad: #b91c1c; }
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; color: var(--ink); background: #f9fafb; }
header { display: flex; gap: 20px; padding: 14px 32px; background: #1e1b4b; }
header a { color: #c7d2fe; text-decoration: none; font-weight: 600; }
header a:first-child { color: #fff; margin-right: auto; }
main { max-width: 820px; margin: 0 auto; padding: 28px 32px 48px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
td, th { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); }
button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 0; background: var(--brand);
  color: #fff; font-weight: 600; cursor: pointer; }
button.danger { background: var(--bad); }
.badge { padding: 3px 10px; border-radius: 999px; font-size: 13px; font-weight: 700; }
.badge.active { background: #d1fae5; color: var(--ok); }
.badge.disabled { background: #fee2e2; color: var(--bad); }
dialog { border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; }
.toast { position: fixed; right: 24px; bottom: 24px; padding: 12px 16px; border-radius: 10px;
  background: #111827; color: #fff; }
.balance { font-size: 30px; font-weight: 750; }
"""


@dataclass(slots=True)
class LedgerState:
    disabled: set[str] = field(default_factory=set[str])


def _page(title: str, body: str, script: str = "") -> HTMLResponse:
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}'
        f" | Ledgerly Admin</title><style>{STYLE}</style></head><body>"
        '<header><a href="/">Ledgerly Admin</a><a href="/users">Users</a>'
        '<a href="/accounts">Accounts</a></header>'
        f"<main>{body}</main>{f'<script>{script}</script>' if script else ''}</body></html>"
    )
    return HTMLResponse(html)


def _state(request: Request) -> LedgerState:
    return request.app.state.ledger


async def home(request: Request) -> Response:
    return _page(
        "Dashboard",
        "<h1>Dashboard</h1><p>4 users, 3 accounts.</p>"
        '<p><a href="/users">Manage users</a> · <a href="/accounts">View accounts</a></p>',
    )


async def users_page(request: Request) -> Response:
    rows = "".join(
        f'<tr><td><a href="/users/{uid}">{escape(name)}</a></td><td>{escape(email)}</td></tr>'
        for uid, (name, email) in USERS.items()
    )
    return _page(
        "Users", f"<h1>Users</h1><table><tr><th>Name</th><th>Email</th></tr>{rows}</table>"
    )


USER_SCRIPT = """
const id = location.pathname.split('/').pop();
async function load() {
  const r = await fetch('/api/users/' + id);
  const u = await r.json();
  document.getElementById('name').textContent = u.name;
  document.getElementById('email').textContent = u.email;
  const badge = document.getElementById('status');
  badge.textContent = u.status === 'disabled' ? 'Disabled' : 'Active';
  badge.className = 'badge ' + u.status;
  document.getElementById('disable').hidden = u.status === 'disabled';
}
document.getElementById('disable').onclick = () => {
  document.getElementById('confirm-name').textContent = document.getElementById('name').textContent;
  document.getElementById('confirm').showModal();
};
document.getElementById('cancel').onclick = () => document.getElementById('confirm').close();
document.getElementById('confirm-disable').onclick = async () => {
  document.getElementById('confirm').close();
  const r = await fetch('/api/users/' + id + '/disable', {method: 'POST'});
  const u = await r.json();
  const toast = document.getElementById('toast');
  toast.textContent = r.ok ? u.name + ' has been disabled.' : 'Could not disable the user.';
  toast.hidden = false;
  setTimeout(() => { toast.hidden = true; }, 4000);
  await load();
};
load();
"""


async def user_page(request: Request) -> Response:
    if request.path_params["user_id"] not in USERS:
        return _page("Not found", "<h1>Not found</h1>")
    body = (
        '<h1 id="name">Loading…</h1><p>Email: <span id="email"></span></p>'
        '<p>Status: <span class="badge" id="status"></span></p>'
        '<p><button class="danger" id="disable" hidden>Disable user</button></p>'
        '<dialog id="confirm"><p>Disable <strong id="confirm-name"></strong>? They will not be '
        'able to sign in.</p><button id="confirm-disable" class="danger">Disable</button> '
        '<button id="cancel">Cancel</button></dialog>'
        '<div class="toast" id="toast" role="status" hidden></div>'
    )
    return _page("User", body, USER_SCRIPT)


async def accounts_page(request: Request) -> Response:
    rows = "".join(
        f'<tr><td><a href="/accounts/{a.id}">{a.id}</a></td><td>{escape(a.owner)}</td>'
        f"<td>{a.kind}</td></tr>"
        for a in ACCOUNTS.values()
    )
    return _page(
        "Accounts",
        "<h1>Accounts</h1><p>Open an account to see its balance.</p>"
        f"<table><tr><th>Account</th><th>Owner</th><th>Type</th></tr>{rows}</table>",
    )


ACCOUNT_SCRIPT = """
const id = location.pathname.split('/').pop();
fetch('/api/accounts/' + id).then(r => r.json()).then(a => {
  document.getElementById('owner').textContent = a.owner + ' · ' + a.kind;
  document.getElementById('balance').textContent = '$' + a.balance;
});
"""


async def account_page(request: Request) -> Response:
    account_id = request.path_params["account_id"]
    if account_id not in ACCOUNTS:
        return _page("Not found", "<h1>Not found</h1>")
    body = (
        f'<h1>Account {escape(account_id)}</h1><p id="owner"></p>'
        '<p>Balance</p><p class="balance" id="balance">…</p>'
    )
    return _page(account_id, body, ACCOUNT_SCRIPT)


def _user_json(request: Request, user_id: str) -> dict[str, str]:
    name, email = USERS[user_id]
    status = "disabled" if user_id in _state(request).disabled else "active"
    return {"id": user_id, "name": name, "email": email, "status": status}


async def api_user(request: Request) -> Response:
    user_id = request.path_params["user_id"]
    if user_id not in USERS:
        return JSONResponse({"error": "unknown user"}, status_code=404)
    return JSONResponse(_user_json(request, user_id))


async def api_disable_user(request: Request) -> Response:
    user_id = request.path_params["user_id"]
    if user_id not in USERS:
        return JSONResponse({"error": "unknown user"}, status_code=404)
    _state(request).disabled.add(user_id)
    return JSONResponse(_user_json(request, user_id))


async def api_account(request: Request) -> Response:
    account = ACCOUNTS.get(request.path_params["account_id"])
    if account is None:
        return JSONResponse({"error": "unknown account"}, status_code=404)
    return JSONResponse(
        {"id": account.id, "owner": account.owner, "kind": account.kind, "balance": account.balance}
    )


async def api_reset(request: Request) -> Response:
    request.app.state.ledger = LedgerState()
    return JSONResponse({"ok": True})


def create_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/", home),
            Route("/users", users_page),
            Route("/users/{user_id}", user_page),
            Route("/accounts", accounts_page),
            Route("/accounts/{account_id}", account_page),
            Route("/api/users/{user_id}", api_user, methods=["GET"]),
            Route("/api/users/{user_id}/disable", api_disable_user, methods=["POST"]),
            Route("/api/accounts/{account_id}", api_account, methods=["GET"]),
            Route("/api/reset", api_reset, methods=["POST"]),
        ]
    )
    app.state.ledger = LedgerState()
    return app


app = create_app()
