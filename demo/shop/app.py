"""Kettle Co.: a tiny shop that browser-agent runs are recorded against.

Run it with ``uv run uvicorn demo.shop.app:app --port 8000``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
    body { font-family: system-ui, sans-serif; margin: 0; color: #222; }
    header { background: #b22222; color: #fff; padding: 12px 24px; display: flex; gap: 24px; }
    header a { color: #fff; text-decoration: none; }
    main { padding: 24px; max-width: 720px; }
    .product { border: 1px solid #ddd; padding: 12px; margin-bottom: 12px; border-radius: 6px; }
    .price { font-weight: bold; }
    button { padding: 8px 16px; font-size: 15px; cursor: pointer; }
    .notice { color: #2e7d32; }
    .error { color: #c62828; }
  </style>
</head>
<body>
  <header>
    <a href="/"><strong>Kettle Co.</strong></a>
    <a href="/cart">Cart</a>
    <a href="/account">Account</a>
  </header>
  <main>{% block main %}{% endblock %}</main>
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
<h1>Kettles</h1>
{% for p in products %}
<div class="product" data-product-id="{{ p.id }}">
  <h2><a href="/products/{{ p.id }}">{{ p.name }}</a></h2>
  <p>{{ p.blurb }}</p>
  <p class="price">${{ "%.2f"|format(p.price) }}</p>
</div>
{% endfor %}
{% endblock %}
""",
    "product.html": """{% extends "base.html" %}
{% block title %}{{ product.name }} | Kettle Co.{% endblock %}
{% block main %}
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
{% endblock %}
{% block scripts %}
<script>
  document.getElementById("add-to-cart").addEventListener("click", async () => {
    const qty = Number(document.getElementById("qty").value);
    const res = await postJson("/api/cart", { product_id: "{{ product.id }}", qty });
    const status = document.getElementById("status");
    status.textContent = res.ok ? "Added to cart." : "Could not add to cart: " + res.body.error;
  });
</script>
{% endblock %}
""",
    "cart.html": """{% extends "base.html" %}
{% block title %}Cart | Kettle Co.{% endblock %}
{% block main %}
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
{% endblock %}
""",
    "checkout.html": """{% extends "base.html" %}
{% block title %}Checkout | Kettle Co.{% endblock %}
{% block main %}
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
<h1>Thank you for your order #{{ order_id }}</h1>
<ul>
{% for line in lines %}
  <li>{{ line.product_id }} x {{ line.qty }} at ${{ "%.2f"|format(line.price) }}</li>
{% endfor %}
</ul>
{% endblock %}
""",
    "account.html": """{% extends "base.html" %}
{% block title %}Account | Kettle Co.{% endblock %}
{% block main %}
<h1>Your account</h1>
<p>Email: <span id="current-email">{{ email }}</span></p>
<label>New email <input id="email" type="email"></label>
<button id="save-email">Save</button>
<p id="status" class="notice"></p>
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
    return payload if isinstance(payload, dict) else None


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
    items = payload.get("items") if payload else None
    if not isinstance(items, list) or not items:
        return _error("an order needs at least one item", 400)
    order_id = state.next_order_id
    state.next_order_id += 1
    state.orders[order_id] = [dict(item) for item in items if isinstance(item, dict)]
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
