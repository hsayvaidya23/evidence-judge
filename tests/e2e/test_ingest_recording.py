from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from evidence_judge.ingest import load_playwright_trace, load_run

pytestmark = pytest.mark.e2e

FORM_PAGE = """
<!doctype html>
<title>Form lab</title>
<h1>Delivery</h1>
<label>Name <input id="name"></label>
<label>Size
  <select id="size">
    <option value="s">Small</option>
    <option value="l">Large</option>
  </select>
</label>
<label><input id="gift" type="checkbox"> Gift wrap</label>
<input id="file" type="file">
<button id="hover-me" onmouseover="this.textContent = 'Hovered'">Hover me</button>
<p style="display: none">Do not show</p>
<div id="host"></div>
<iframe srcdoc="<p>inside the frame</p>"></iframe>
<script>
  const root = document.getElementById("host").attachShadow({ mode: "open" });
  root.innerHTML = "<p>shadow text</p>";
</script>
"""


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    from scripts.record_fixtures import SCENARIOS, record, running_shop

    out = tmp_path_factory.mktemp("runs")
    wanted = {"honest_success", "forged_success"}
    with running_shop() as base:
        for scenario in SCENARIOS:
            if scenario.name in wanted:
                record(scenario, base, out)
    yield out


def test_fresh_recording_of_honest_success(recorded: Path) -> None:
    d = recorded / "honest_success"

    run = load_run(trace=d / "trace.zip", har=d / "run.har", answer_file=d / "answer.txt")

    assert any(n.method == "POST" and n.url.endswith("/api/orders") and n.ok for n in run.network)
    assert run.final_state is not None
    assert "Thank you for your order #1001" in run.final_state.text
    assert run.final_state.screenshot_id is not None
    assert run.agent_answer is not None and "#1001" in run.agent_answer


def test_fresh_recording_of_forged_success(recorded: Path) -> None:
    run = load_playwright_trace(recorded / "forged_success" / "trace.zip")

    assert not [n for n in run.network if "/api/orders" in n.url]
    assert run.final_state is not None
    assert "Thank you" not in run.final_state.text


def test_form_actions_and_snapshot_decoding(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    upload = tmp_path / "receipt.txt"
    upload.write_text("receipt")
    trace = tmp_path / "trace.zip"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 800, "height": 600})
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.route(
            "https://form.test/",
            lambda route: route.fulfill(content_type="text/html", body=FORM_PAGE),
        )
        page.goto("https://form.test/")
        page.fill("#name", "Sam")
        page.press("#name", "Enter")
        page.locator("#name").press_sequentially("!")
        page.select_option("#size", "l")
        page.check("#gift")
        page.uncheck("#gift")
        page.set_input_files("#file", upload)
        page.hover("#hover-me")
        page.evaluate("console.warn('careful')")
        context.tracing.stop(path=trace)
        context.close()
        browser.close()

    run = load_playwright_trace(trace)

    assert [(a.name, a.selector, a.value) for a in run.actions] == [
        ("goto", None, "https://form.test/"),
        ("fill", "#name", "Sam"),
        ("press", "#name", "Enter"),
        ("fill", "#name", "!"),
        ("select", "#size", "l"),
        ("check", "#gift", None),
        ("uncheck", "#gift", None),
        ("upload", "#file", "receipt.txt"),
        ("hover", "#hover-me", None),
        ("inject", None, "evaluate(console.warn('careful'))"),
    ]
    assert [n.synthetic for n in run.network] == [True]
    assert [(c.level, c.text) for c in run.console] == [("warning", "careful")]
    final = run.final_state
    assert final is not None
    assert final.title == "Form lab"
    assert "Delivery Name Size Large Gift wrap" in final.text
    assert "Sam" not in final.text
    assert run.metadata["form_values_excluded"] > 0
    assert "Hovered" in final.text
    assert "shadow text" in final.text
    assert "Small" not in final.text
    assert "Do not show" not in final.text
    assert "inside the frame" not in final.text


def test_final_state_stays_on_the_main_tab(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    pages = {
        "https://shop.test/": "<title>Shop</title><a id='go' href='/cart'>Cart</a>",
        "https://shop.test/cart": "<title>Cart</title><p>Order #1042 pending</p>",
        "https://ads.test/": "<title>Ad</title><p>Order #1042 confirmed</p>",
    }
    trace = tmp_path / "trace.zip"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        context.route(
            "https://*.test/**",
            lambda route: route.fulfill(content_type="text/html", body=pages[route.request.url]),
        )
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.goto("https://shop.test/")
        page.click("#go")
        page.wait_for_url("https://shop.test/cart")
        with context.expect_page() as popup:
            page.evaluate("window.open('https://ads.test/')")
        popup.value.wait_for_load_state()
        popup.value.evaluate("document.title")
        context.tracing.stop(path=trace)
        context.close()
        browser.close()

    run = load_playwright_trace(trace)

    assert run.snapshots[-1].url == "https://ads.test/"
    final = run.final_state
    assert final is not None
    assert (final.url, final.text) == ("https://shop.test/cart", "Order #1042 pending")

