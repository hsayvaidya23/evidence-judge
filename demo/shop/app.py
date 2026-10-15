"""Kettle Co.: a tiny shop that browser-agent runs are recorded against.

Run it with ``uv run uvicorn demo.shop.app:app --port 8000``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from jinja2 import DictLoader, Environment, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    name: str
    price: float
    blurb: str


CATALOGUE: tuple[Product, ...] = (
    Product(
        "red-kettle", "Red Kettle", 24.00, "A cheerful 1.5 L stovetop kettle in pillar-box red."
    ),
    Product(
        "steel-kettle", "Steel Kettle", 45.00, "Brushed stainless steel, 1.7 L, whistling spout."
    ),
    Product("blue-kettle", "Blue Kettle", 29.00, "Enamel 1.2 L kettle in deep cobalt blue."),
)
PRODUCTS: dict[str, Product] = {p.id: p for p in CATALOGUE}
FIRST_ORDER_ID = 1001
DEFAULT_EMAIL = "sam@example.com"


@dataclass(slots=True)
class ShopState:
    cart: dict[str, int] = field(default_factory=dict[str, int])
    orders: dict[int, list[dict[str, Any]]] = field(default_factory=dict[int, list[dict[str, Any]]])
    next_order_id: int = FIRST_ORDER_ID
    email: str = DEFAULT_EMAIL
    out_of_stock: set[str] = field(default_factory=set[str])

    def cart_lines(self) -> list[dict[str, Any]]:
        return [
            {
                "product_id": pid,
                "name": PRODUCTS[pid].name,
                "qty": qty,
                "price": PRODUCTS[pid].price,
            }
            for pid, qty in self.cart.items()
        ]


TEMPLATES = {
    "base.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{% block title %}Kettle Co.{% endblock %}</title>
  <style>
    :root {
      --ink: #1a1714; --ink-2: #5b544c; --ink-3: #8a8178; --paper: #faf7f2; --card: #ffffff;
      --line: #ece5db; --brand: #c2410c; --brand-ink: #fff7ed; --ok: #15803d; --bad: #be123c;
      --shadow: 0 1px 2px rgba(26, 23, 20, 0.05), 0 18px 40px -22px rgba(26, 23, 20, 0.35);
      --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body { font-family: var(--font); margin: 0; color: var(--ink); background: var(--paper);
      -webkit-font-smoothing: antialiased; }
    header { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 26px;
      padding: 14px 36px; background: rgba(250, 247, 242, 0.86); backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--line); font-size: 14.5px; }
    header a { color: var(--ink-2); text-decoration: none; font-weight: 550; }
    header a:hover { color: var(--ink); }
    header a:first-child { margin-right: auto; display: inline-flex; align-items: center; gap: 10px;
      color: var(--ink); font-size: 17px; letter-spacing: -0.02em; }
    header a:first-child::before { content: ""; width: 26px; height: 26px; border-radius: 9px;
      background: radial-gradient(circle at 30% 30%, #fb923c, var(--brand) 70%);
      box-shadow: inset 0 -3px 6px rgba(0, 0, 0, 0.18), 0 4px 10px -4px var(--brand); }
    header a[href="/cart"] { padding: 7px 14px; border-radius: 999px; background: var(--ink);
      color: #fff; }
    main { padding: 34px 36px 56px; max-width: 1040px; margin: 0 auto; }
    h1 { font-size: 34px; line-height: 1.12; letter-spacing: -0.035em; margin: 0 0 10px;
      font-weight: 750; }
    h2 { font-size: 19px; letter-spacing: -0.02em; margin: 16px 0 4px; }
    h2 a { color: var(--ink); text-decoration: none; }
    p { color: var(--ink-2); line-height: 1.55; }
    .hero { display: grid; gap: 6px; padding: 30px 32px; margin-bottom: 26px; border-radius: 22px;
      color: #fff7ed; background: radial-gradient(520px 260px at 88% 10%, rgba(251, 146, 60, 0.55), transparent 70%),
        linear-gradient(120deg, #1a1714 0%, #3b1d12 100%); box-shadow: var(--shadow); }
    .hero .eyebrow { font-size: 12px; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase;
      color: #fdba74; }
    .hero h1 { color: #fff; font-size: 38px; margin: 0; }
    .hero p { color: rgba(255, 247, 237, 0.75); margin: 2px 0 0; max-width: 460px; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; }
    .product { position: relative; padding: 14px 14px 18px; border-radius: 20px; background: var(--card);
      border: 1px solid var(--line); box-shadow: var(--shadow); transition: transform 0.2s; }
    .product:hover { transform: translateY(-2px); }
    .product p { margin: 4px 0 0; font-size: 14px; }
    .product h2 a::after { content: ""; position: absolute; inset: 0; border-radius: 20px; }
    .price { font-weight: 750; color: var(--ink); font-size: 17px; letter-spacing: -0.01em; }
    .product .price { margin-top: 12px; }
    [data-product-id="red-kettle"] { --c: #e11d48; --c2: #fb7185; --bg: #fff1f2; }
    [data-product-id="steel-kettle"] { --c: #64748b; --c2: #cbd5e1; --bg: #f1f5f9; }
    [data-product-id="blue-kettle"] { --c: #1d4ed8; --c2: #60a5fa; --bg: #eff6ff; }
    .art { position: relative; display: grid; place-items: center; height: 190px; border-radius: 14px;
      background: radial-gradient(circle at 50% 115%, color-mix(in srgb, var(--c2) 45%, transparent), transparent 62%), var(--bg, #f4efe8); overflow: hidden; }
    .art-lg { height: 360px; border-radius: 22px; }
    .kettle { position: relative; width: 112px; height: 86px; margin-top: 26px;
      border-radius: 48% 48% 22px 22px / 62% 62% 22px 22px;
      background: linear-gradient(140deg, var(--c2) 0%, var(--c) 55%, color-mix(in srgb, var(--c) 70%, #000) 100%);
      box-shadow: inset -10px -10px 22px rgba(0, 0, 0, 0.18), inset 10px 8px 16px rgba(255, 255, 255, 0.35),
        0 22px 26px -18px rgba(0, 0, 0, 0.55); }
    .kettle::before { content: ""; position: absolute; left: 22px; top: -38px; width: 68px; height: 54px;
      border: 7px solid #1a1714; border-bottom: 0; border-radius: 40px 40px 0 0; }
    .kettle::after { content: ""; position: absolute; right: -30px; top: 24px; width: 44px; height: 15px;
      border-radius: 0 9px 9px 0; background: var(--c); transform: rotate(-32deg); transform-origin: left center; }
    .kettle i { position: absolute; left: 46px; top: -9px; width: 20px; height: 12px; border-radius: 6px 6px 2px 2px;
      background: #1a1714; }
    .art-lg .kettle { transform: scale(1.7); }
    .pdp { display: grid; grid-template-columns: 1.1fr 1fr; gap: 40px; align-items: center; }
    .pdp .price { font-size: 26px; margin: 8px 0 18px; }
    label { display: inline-flex; align-items: center; gap: 10px; margin-right: 12px; color: var(--ink-2);
      font-size: 14px; font-weight: 550; }
    input { font: inherit; padding: 10px 12px; border-radius: 12px; border: 1px solid var(--line);
      background: #fff; color: var(--ink); width: 76px; }
    input[type="email"] { width: 260px; }
    button, #checkout { display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 12px 22px; font: 650 15px/1 var(--font); cursor: pointer; border: 0; border-radius: 999px;
      color: #fff; background: linear-gradient(180deg, #ea580c, var(--brand)); text-decoration: none;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.25), 0 10px 22px -10px var(--brand); }
    button:disabled { background: #d6d0c8; color: #8a8178; box-shadow: none; cursor: not-allowed; }
    .panel { max-width: 560px; padding: 24px 26px; border-radius: 20px; background: var(--card);
      border: 1px solid var(--line); box-shadow: var(--shadow); }
    .panel ul { list-style: none; margin: 0 0 6px; padding: 0; }
    .panel li { display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line);
      font-weight: 600; }
    .panel li::before { content: ""; width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
      background: radial-gradient(circle at 35% 30%, #fdba74, var(--brand) 75%); }
    .notice { color: var(--ok); font-weight: 600; }
    .error { color: var(--bad); font-weight: 600; }
    .done { text-align: center; margin: 0 auto; }
    .done::before { content: "✓"; display: grid; place-items: center; width: 56px; height: 56px;
      margin: 0 auto 14px; border-radius: 50%; background: #dcfce7; color: var(--ok); font-size: 28px; font-weight: 800; }
    footer { padding: 26px 36px 34px; color: var(--ink-3); font-size: 13px; border-top: 1px solid var(--line);
      text-align: center; }
    @media (max-width: 760px) {
      header { padding: 12px 18px; gap: 16px; }
      main { padding: 22px 18px 40px; }
      .grid, .pdp { grid-template-columns: 1fr; }
      .art-lg { height: 240px; }
    }
  </style>
</head>
<body>
  <header>
    <a href="/"><strong>Kettle Co.</strong></a>
    <a href="/cart">Cart</a>
    <a href="/account">Account</a>
  </header>
  <main>{% block main %}{% endblock %}</main>
  <footer>Small-batch kettles · free returns · carbon-neutral shipping</footer>
  <div hidden>Internal: promo code KETTLE10 is not active.</div>
  <script>
    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return { ok: res.ok, status: res.status, body: await res.json() };
    }
  </script>
  {% block scripts %}{% endblock %}
</body>
</html>
""",
    "home.html": """{% extends "base.html" %}
{% block main %}
<section class="hero">
  <span class="eyebrow">New season · Stovetop collection</span>
  <h1>Kettles</h1>
  <p>Enamel and steel, made to whistle for years. Pick a colour for slow mornings.</p>
</section>
<div class="grid">
{% for p in products %}
<div class="product" data-product-id="{{ p.id }}">
  <div class="art" aria-hidden="true"><span class="kettle"><i></i></span></div>
  <h2><a href="/products/{{ p.id }}">{{ p.name }}</a></h2>
  <p>{{ p.blurb }}</p>
  <p class="price">${{ "%.2f"|format(p.price) }}</p>
</div>
{% endfor %}
</div>
{% endblock %}
""",
    "product.html": """{% extends "base.html" %}
{% block title %}{{ product.name }} | Kettle Co.{% endblock %}
{% block main %}
<div class="pdp" data-product-id="{{ product.id }}">
<div class="art art-lg" aria-hidden="true"><span class="kettle"><i></i></span></div>
<div>
<h1>{{ product.name }}</h1>
<p>{{ product.blurb }}</p>
<p class="price">${{ "%.2f"|format(product.price) }}</p>
{% if in_stock %}
<label>Quantity <input id="qty" type="number" min="1" value="1"></label>
<button id="add-to-cart">Add to cart</button>
{% else %}
<p class="error">Out of stock</p>
<button id="add-to-cart" disabled>Add to cart</button>
{% endif %}
<p id="status" class="notice"></p>
</div>
</div>
{% endblock %}
{% block scripts %}
<script>
  document.getElementById("add-to-cart").addEventListener("click", async () => {
    const qty = Number(document.getElementById("qty").value);
    const res = await postJson("/api/cart", { product_id: "{{ product.id }}", qty });
    const status = document.getElementById("status");
    status.textContent = res.ok ? "Added to cart." : "Could not add to cart: " + res.body.error;
    console.info("cart updated", res.status, JSON.stringify(res.body));
  });
</script>
{% endblock %}
""",
    "cart.html": """{% extends "base.html" %}
{% block title %}Cart | Kettle Co.{% endblock %}
{% block main %}
<div class="panel">
<h1>Your cart</h1>
{% if lines %}
<ul>
{% for line in lines %}
  <li>{{ line.name }} x {{ line.qty }} at ${{ "%.2f"|format(line.price) }}</li>
{% endfor %}
</ul>
<p>Total: <span class="price">${{ "%.2f"|format(total) }}</span></p>
<a id="checkout" href="/checkout">Proceed to checkout</a>
{% else %}
<p>Your cart is empty.</p>
{% endif %}
</div>
{% endblock %}
""",
    "checkout.html": """{% extends "base.html" %}
{% block title %}Checkout | Kettle Co.{% endblock %}
{% block main %}
<div class="panel">
<h1>Checkout</h1>
{% if lines %}
<ul>
{% for line in lines %}
  <li>{{ line.name }} x {{ line.qty }} at ${{ "%.2f"|format(line.price) }}</li>
{% endfor %}
</ul>
<p>Total: <span class="price">${{ "%.2f"|format(total) }}</span></p>
<p>Ship to: {{ email }}</p>
<button id="confirm-order">Confirm order</button>
<p id="status" class="error"></p>
{% else %}
<p>Your cart is empty.</p>
{% endif %}
</div>
{% endblock %}
{% block scripts %}
<script id="cart-data" type="application/json">{{ lines_json|safe }}</script>
<script>
  const button = document.getElementById("confirm-order");
  if (button) {
    button.addEventListener("click", async () => {
      const items = JSON.parse(document.getElementById("cart-data").textContent);
      const res = await postJson("/api/orders", {
        items: items.map(({ product_id, qty, price }) => ({ product_id, qty, price })),
      });
      if (res.ok) {
        window.location.href = "/orders/" + res.body.order_id;
      } else {
        document.getElementById("status").textContent = "Order failed: " + res.body.error;
      }
    });
  }
</script>
{% endblock %}
""",
    "order.html": """{% extends "base.html" %}
{% block title %}Order #{{ order_id }} | Kettle Co.{% endblock %}
{% block main %}
<div class="panel done">
<h1>Thank you for your order #{{ order_id }}</h1>
<ul>
{% for line in lines %}
  <li>{{ line.product_id }} x {{ line.qty }} at ${{ "%.2f"|format(line.price) }}</li>
{% endfor %}
</ul>
</div>
{% endblock %}
""",
    "account.html": """{% extends "base.html" %}
{% block title %}Account | Kettle Co.{% endblock %}
{% block main %}
<div class="panel">
<h1>Your account</h1>
<p>Email: <span id="current-email">{{ email }}</span></p>
<label>New email <input id="email" type="email"></label>
<button id="save-email">Save</button>
<p id="status" class="notice"></p>
</div>
{% endblock %}
{% block scripts %}
<script>
  document.getElementById("save-email").addEventListener("click", async () => {
    const email = document.getElementById("email").value;
    const res = await postJson("/api/account/email", { email });
    if (res.ok) {
      document.getElementById("current-email").textContent = res.body.email;
      document.getElementById("status").textContent = "Email updated.";
    } else {
      document.getElementById("status").textContent = "Could not update: " + res.body.error;
    }
  });
</script>
{% endblock %}
""",
    "not_found.html": """{% extends "base.html" %}
{% block title %}Not found | Kettle Co.{% endblock %}
{% block main %}<h1>Not found</h1>{% endblock %}
""",
}

_env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(default=True))


def _render(name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(**context), status_code=status_code)


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _state(request: Request) -> ShopState:
    return request.app.state.shop


def _total(lines: list[dict[str, Any]]) -> float:
    return sum(line["qty"] * line["price"] for line in lines)


async def home(request: Request) -> Response:
    return _render("home.html", products=CATALOGUE)


async def product_page(request: Request) -> Response:
    product = PRODUCTS.get(request.path_params["product_id"])
    if product is None:
        return _render("not_found.html", status_code=404)
    in_stock = product.id not in _state(request).out_of_stock
    return _render("product.html", product=product, in_stock=in_stock)


async def cart_page(request: Request) -> Response:
    lines = _state(request).cart_lines()
    return _render("cart.html", lines=lines, total=_total(lines))


async def checkout_page(request: Request) -> Response:
    state = _state(request)
    lines = state.cart_lines()
    lines_json = json.dumps(lines).replace("</", "<\\/")
    return _render(
        "checkout.html", lines=lines, total=_total(lines), email=state.email, lines_json=lines_json
    )


async def order_page(request: Request) -> Response:
    order_id = int(request.path_params["order_id"])
    lines = _state(request).orders.get(order_id)
    if lines is None:
        return _render("not_found.html", status_code=404)
    return _render("order.html", order_id=order_id, lines=lines)


async def account_page(request: Request) -> Response:
    return _render("account.html", email=_state(request).email)


async def api_get_cart(request: Request) -> Response:
    return JSONResponse({"items": _state(request).cart_lines()})


async def api_set_cart_line(request: Request) -> Response:
    state = _state(request)
    payload = await _json_object(request)
    if payload is None:
        return _error("expected a JSON object", 400)
    product_id = payload.get("product_id")
    qty = payload.get("qty", 1)
    if product_id not in PRODUCTS:
        return _error("unknown product", 404)
    if not isinstance(qty, int) or qty < 1:
        return _error("qty must be a positive integer", 400)
    if product_id in state.out_of_stock:
        return _error("out of stock", 409)
    state.cart[product_id] = qty
    return JSONResponse({"items": state.cart_lines()})


async def api_create_order(request: Request) -> Response:
    state = _state(request)
    payload = await _json_object(request)
    items = cast(list[object], payload.get("items")) if payload else None
    if not isinstance(items, list) or not items:
        return _error("an order needs at least one item", 400)
    lines = [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]
    order_id = state.next_order_id
    state.next_order_id += 1
    state.orders[order_id] = lines
    state.cart.clear()
    return JSONResponse({"order_id": order_id}, status_code=201)


async def api_change_email(request: Request) -> Response:
    state = _state(request)
    payload = await _json_object(request)
    email = payload.get("email") if payload else None
    if not isinstance(email, str) or "@" not in email:
        return _error("a valid email is required", 400)
    state.email = email
    return JSONResponse({"email": email})


async def api_reset(request: Request) -> Response:
    payload = await _json_object(request) or {}
    out_of_stock = payload.get("out_of_stock", [])
    request.app.state.shop = ShopState(out_of_stock=set(out_of_stock))
    return JSONResponse({"ok": True})


def create_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/", home),
            Route("/products/{product_id}", product_page),
            Route("/cart", cart_page),
            Route("/checkout", checkout_page),
            Route("/orders/{order_id:int}", order_page),
            Route("/account", account_page),
            Route("/api/cart", api_get_cart, methods=["GET"]),
            Route("/api/cart", api_set_cart_line, methods=["POST"]),
            Route("/api/orders", api_create_order, methods=["POST"]),
            Route("/api/account/email", api_change_email, methods=["POST"]),
            Route("/api/reset", api_reset, methods=["POST"]),
        ]
    )
    app.state.shop = ShopState()
    return app


app = create_app()
