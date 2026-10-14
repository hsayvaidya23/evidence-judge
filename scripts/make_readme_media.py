"""Render the README media into docs/assets/: report screenshots, two demo animations and the
social preview card.

Reports come from the real CLI run offline on the recorded fixtures; the agent animation drives
the demo shop through the forged_success scenario. Every frame is a Playwright screenshot with
a fixed duration, so the output is crisp and the same on every run.

    uv run --with pillow python -m scripts.make_readme_media [--only reports agent verdict social]
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image  # pyright: ignore[reportMissingImports]
from playwright.sync_api import Browser, Page, Response, ViewportSize, sync_playwright
from scripts.record_fixtures import running_shop

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "tests" / "fixtures" / "runs"
DEFAULT_OUT = ROOT / "docs" / "assets"

PNG_BUDGET = 400 * 1024
ANIMATION_BUDGET = 3 * 1024 * 1024
REPORT_WIDTH = 1200
REPORT_CROP = 1400
MOBILE: ViewportSize = {"width": 390, "height": 844}
MOBILE_CROP = 1500
FRAME: ViewportSize = {"width": 960, "height": 600}
CARD: ViewportSize = {"width": 1280, "height": 640}
SHOP_HOST = "localhost:8000"

Scheme = Literal["light", "dark"]
Step = Literal["reports", "agent", "verdict", "social"]
STEPS: tuple[Step, ...] = ("reports", "agent", "verdict", "social")

OVERLAY_JS = r"""
(() => {
  const opts = window.__EJ_OPTS || {};
  const css = `
  .ej-chrome { position: fixed; inset: 0 0 auto 0; height: 44px; z-index: 2147483000;
    display: flex; align-items: center; gap: 14px; padding: 0 16px; background: #24272e;
    border-bottom: 1px solid #111; font: 500 14px/1 system-ui, -apple-system, sans-serif; }
  .ej-dots { display: flex; gap: 8px; }
  .ej-dots i { width: 12px; height: 12px; border-radius: 50%; display: block; }
  .ej-url { flex: 1; max-width: 520px; margin: 0 auto; padding: 7px 14px; border-radius: 8px;
    background: #353942; color: #d8dbe2; letter-spacing: 0.01em; }
  .ej-url b { color: #fff; font-weight: 500; }
  .ej-cap { position: fixed; left: 0; right: 0; bottom: 22px; margin: 0 auto; width: fit-content;
    z-index: 2147483001; display: flex; align-items: center; gap: 12px; max-width: 92%;
    padding: 12px 20px 12px 12px; border-radius: 14px; background: rgba(15, 18, 25, 0.94);
    color: #fff; font: 600 21px/1.3 system-ui, -apple-system, sans-serif;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35); letter-spacing: -0.005em; }
  .ej-cap[hidden] { display: none; }
  .ej-cap .ej-who { flex-shrink: 0; padding: 5px 9px; border-radius: 8px; font-size: 12px;
    font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; }
  .ej-who.agent { background: #f59e0b; color: #1a1205; }
  .ej-who.judge { background: #6d8bff; color: #0b1230; }
  .ej-who.task { background: #e5e7eb; color: #111827; }
  .ej-who.bad { background: #ef4444; color: #fff; }
  .ej-cursor { position: fixed; left: 0; top: 0; width: 26px; height: 26px; z-index: 2147483003;
    pointer-events: none; filter: drop-shadow(0 2px 3px rgba(0, 0, 0, 0.45)); }
  .ej-cursor[hidden] { display: none; }
  .ej-ring { position: fixed; width: 34px; height: 34px; margin: -17px 0 0 -17px;
    border-radius: 50%; border: 3px solid #f59e0b; background: rgba(245, 158, 11, 0.25);
    z-index: 2147483002; pointer-events: none; }
  .ej-ring[hidden] { display: none; }
  .ej-net { position: fixed; right: 16px; top: 104px; width: 300px; z-index: 2147483002;
    padding: 10px 12px 12px; border-radius: 12px; background: rgba(15, 18, 25, 0.92);
    color: #cfd4dd; font: 13px/1.6 ui-monospace, "SF Mono", Menlo, monospace;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3); }
  .ej-net[hidden] { display: none; }
  .ej-net h6 { margin: 0 0 6px; font: 700 11px/1 system-ui, sans-serif; letter-spacing: 0.1em;
    text-transform: uppercase; color: #8b93a2; }
  .ej-net div { display: flex; gap: 8px; white-space: nowrap; }
  .ej-net .m { width: 38px; color: #8b93a2; }
  .ej-net .p { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .ej-net .s { color: #5fd08e; }
  .ej-net .post .m, .ej-net .post .p { color: #fbbf24; font-weight: 700; }
  .ej-net .none { color: #ff8a80; font-weight: 700; }
  .ej-hl { outline: 3px solid var(--ej-hl, #f59e0b) !important; outline-offset: 3px !important;
    border-radius: 8px; box-shadow: 0 0 0 9px color-mix(in srgb, var(--ej-hl, #f59e0b) 22%, transparent) !important; }
  tr.ej-hl { outline: none !important; box-shadow: none !important; }
  tr.ej-hl td { background: color-mix(in srgb, var(--ej-hl) 16%, transparent) !important; }
  tr.ej-hl td:first-child { box-shadow: inset 4px 0 0 var(--ej-hl) !important; }
  .ej-tag { position: fixed; z-index: 2147483001; padding: 5px 10px; border-radius: 8px;
    background: #ef4444; color: #fff; font: 700 14px/1.2 system-ui, sans-serif;
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25); white-space: nowrap; }
  .ej-dim { position: fixed; inset: 0; z-index: 2147483001; display: flex; align-items: center;
    padding: 0 0 0 36px; background: rgba(8, 10, 14, 0.62); }
  .ej-dim[hidden] { display: none; }
  .ej-bubble { position: relative; max-width: 560px; margin: 0 0 40px; padding: 26px 32px 28px;
    border-radius: 22px; background: #fff; color: #111827;
    font: 15px/1.5 system-ui, -apple-system, sans-serif; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45); }
  .ej-bubble::after { content: ""; position: absolute; left: 70px; bottom: -22px;
    border: 12px solid transparent; border-top: 14px solid #fff; border-left: 18px solid #fff; }
  .ej-bubble .ej-head { display: flex; align-items: center; gap: 12px; color: #6b7280;
    font-weight: 650; font-size: 15px; letter-spacing: 0.02em; }
  .ej-bubble .ej-av { width: 40px; height: 40px; border-radius: 50%; display: grid;
    place-items: center; background: #fef3c7; font-size: 22px; }
  .ej-bubble .ej-say { margin: 14px 0 0; font-size: 31px; line-height: 1.25; font-weight: 750;
    letter-spacing: -0.02em; }
  .ej-bubble .ej-foot { margin: 14px 0 0; color: #6b7280; font-size: 16px; }
  `;
  const cursorSvg = `<svg viewBox="0 0 26 26" xmlns="http://www.w3.org/2000/svg">
    <path d="M4 2 L4 21 L9 16.5 L12.5 24 L16 22.4 L12.6 15 L19 15 Z" fill="#111" stroke="#fff"
      stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  const els = {};
  function mount() {
    if (els.cap || !document.body) return;
    const style = document.createElement("style");
    style.textContent = css + (opts.chrome ? "body { padding-top: 44px !important; }" : "");
    document.head.appendChild(style);
    const add = (tag, cls, html) => {
      const el = document.createElement(tag);
      el.className = cls;
      if (html) el.innerHTML = html;
      document.body.appendChild(el);
      return el;
    };
    if (opts.chrome) {
      els.chrome = add("div", "ej-chrome",
        '<span class="ej-dots"><i style="background:#ff5f57"></i><i style="background:#febc2e">' +
        '</i><i style="background:#28c840"></i></span><span class="ej-url"></span>');
    }
    els.net = add("div", "ej-net");
    els.cap = add("div", "ej-cap");
    els.ring = add("div", "ej-ring");
    els.cursor = add("div", "ej-cursor", cursorSvg);
    els.dim = add("div", "ej-dim");
    els.tags = [];
  }
  function text(el, value) { el.textContent = value; return el; }
  function sync(state) {
    mount();
    if (els.chrome) {
      const url = els.chrome.querySelector(".ej-url");
      url.innerHTML = "";
      url.append(text(document.createElement("b"), opts.chrome), location.pathname);
    }
    const cap = state.caption;
    els.cap.hidden = !cap;
    if (cap) {
      els.cap.innerHTML = "";
      els.cap.append(text(document.createElement("span"), cap.who), cap.text);
      els.cap.firstChild.className = "ej-who " + cap.tone;
    }
    const cur = state.cursor;
    els.cursor.hidden = !cur;
    els.ring.hidden = !(cur && cur.down);
    if (cur) {
      els.cursor.style.transform = `translate(${cur.x - 4}px, ${cur.y - 2}px)`;
      els.ring.style.left = cur.x + "px";
      els.ring.style.top = cur.y + "px";
    }
    els.net.hidden = !state.net;
    if (state.net) {
      els.net.innerHTML = "";
      els.net.append(text(document.createElement("h6"), "Recorded network"));
      for (const row of state.net) {
        const line = document.createElement("div");
        if (row.method === "POST") line.className = "post";
        if (row.none) {
          line.append(text(document.createElement("span"), row.none));
          line.firstChild.className = "none";
        } else {
          for (const [cls, value] of [["m", row.method], ["p", row.path], ["s", row.status]]) {
            const span = text(document.createElement("span"), value);
            span.className = cls;
            line.append(span);
          }
        }
        els.net.append(line);
      }
    }
    document.querySelectorAll(".ej-hl").forEach((el) => el.classList.remove("ej-hl"));
    for (const sel of state.highlight || []) {
      document.querySelectorAll(sel).forEach((el) => el.classList.add("ej-hl"));
    }
    document.documentElement.style.setProperty("--ej-hl", state.hlColor || "#f59e0b");
    els.tags.forEach((el) => el.remove());
    els.tags = [];
    for (const tag of state.tags || []) {
      const target = document.querySelector(tag.selector);
      if (!target) continue;
      const box = target.getBoundingClientRect();
      const el = text(document.createElement("div"), tag.text);
      el.className = "ej-tag";
      el.style.left = box.right + 18 + "px";
      el.style.top = box.top + box.height / 2 - 14 + "px";
      document.body.appendChild(el);
      els.tags.push(el);
    }
    const bubble = state.bubble;
    els.dim.hidden = !bubble;
    if (bubble) {
      els.dim.innerHTML = "";
      const card = document.createElement("div");
      card.className = "ej-bubble";
      const head = document.createElement("div");
      head.className = "ej-head";
      const av = text(document.createElement("span"), "\u{1F916}");
      av.className = "ej-av";
      head.append(av, bubble.title);
      const say = text(document.createElement("p"), bubble.text);
      say.className = "ej-say";
      const foot = text(document.createElement("p"), bubble.foot);
      foot.className = "ej-foot";
      card.append(head, say, foot);
      els.dim.append(card);
    }
  }
  window.__ej = { sync };
})();
"""

SOCIAL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
:root {
  --bg: #0b0e14;
  --ink: #f2f4f8;
  --ink-2: #aab2c0;
  --ink-3: #6f7889;
  --panel: #141924;
  --line: #262d3b;
  --ok: #5fd08e;
  --bad: #ff6b5e;
  --accent: #8ea5ff;
}
* { box-sizing: border-box; }
html, body { margin: 0; width: 1280px; height: 640px; overflow: hidden; }
body {
  background:
    radial-gradient(900px 500px at 85% 20%, rgba(142, 165, 255, 0.16), transparent 60%),
    radial-gradient(700px 500px at 10% 110%, rgba(255, 107, 94, 0.12), transparent 60%),
    var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  display: grid;
  grid-template-columns: 1fr 520px;
  gap: 48px;
  padding: 72px 72px 64px 80px;
}
.brand { display: flex; align-items: center; gap: 14px; color: var(--ink-2);
  font-weight: 650; font-size: 22px; letter-spacing: 0.01em; }
.mark { width: 44px; height: 44px; border-radius: 12px; display: grid; place-items: center;
  background: linear-gradient(135deg, #6d8bff, #a78bfa); color: #0b0e14; font-weight: 800;
  font-size: 22px; }
h1 { margin: 44px 0 0; font-size: 82px; line-height: 0.98; letter-spacing: -0.045em;
  font-weight: 800; }
.tag { margin: 28px 0 0; font-size: 31px; line-height: 1.3; color: var(--ink-2);
  letter-spacing: -0.012em; font-weight: 500; max-width: 560px; }
.tag b { color: var(--ink); font-weight: 700; }
.foot { margin-top: 40px; display: flex; gap: 10px; flex-wrap: wrap; }
.pill { padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line);
  color: var(--ink-2); font-size: 17px; background: rgba(20, 25, 36, 0.8); }
.card { position: relative; align-self: center; padding: 26px 26px 22px; border-radius: 22px;
  background: var(--panel); border: 1px solid var(--line);
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45); }
.who { font-size: 14px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
  color: #f59e0b; }
.claim { margin: 10px 0 0; font-size: 27px; line-height: 1.3; font-weight: 700;
  letter-spacing: -0.015em; }
.ev { margin: 22px 0 0; padding: 0; list-style: none; display: grid; gap: 9px; }
.ev li { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 11px;
  background: #0f131c; border: 1px solid var(--line);
  font: 17px/1.2 ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink-2); }
.ev .id { color: var(--accent); min-width: 58px; }
.ev .st { margin-left: auto; color: var(--ok); }
.ev li.miss { border-color: rgba(255, 107, 94, 0.5); background: rgba(255, 107, 94, 0.08);
  color: #ffd2cd; }
.ev li.miss .id, .ev li.miss .st, .ev .st.neg { color: var(--bad); }
.stamp { position: absolute; right: -26px; top: -34px; transform: rotate(9deg);
  padding: 8px 22px 10px; border: 6px solid var(--bad); border-radius: 14px; color: var(--bad);
  font-size: 58px; font-weight: 900; letter-spacing: 0.08em; background: rgba(11, 14, 20, 0.88);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5); }
</style>
</head>
<body>
<div>
  <div class="brand"><span class="mark">EJ</span>evidence-judge</div>
  <h1>Evidence Judge</h1>
  <p class="tag"><b>Don't trust what the agent says.</b> Check what it did.</p>
  <div class="foot">
    <span class="pill">Playwright traces</span>
    <span class="pill">HAR</span>
    <span class="pill">browser-use</span>
    <span class="pill">offline &amp; deterministic</span>
  </div>
</div>
<div class="card">
  <div class="stamp">FAIL</div>
  <div class="who">Agent claims</div>
  <p class="claim">“I placed the order, order #1042 confirmed.”</p>
  <ul class="ev">
    <li><span class="id">net#3</span>POST /api/cart<span class="st">200</span></li>
    <li><span class="id">net#5</span>GET /checkout<span class="st">200</span></li>
    <li class="miss"><span class="id">✗</span>POST /api/orders<span class="st">never sent</span></li>
    <li><span class="id">final</span>/checkout<span class="st neg">no #1042</span></li>
  </ul>
</div>
</body>
</html>
"""


@dataclass(frozen=True, slots=True)
class ReportShot:
    scenario: str
    out: str
    scheme: Scheme
    viewport: ViewportSize
    scale: int
    crop: int


REPORT_SCENARIOS = ("forged_success", "honest_success", "side_effect")
REPORT_SHOTS: tuple[ReportShot, ...] = (
    ReportShot("forged_success", "report-fail-light.png", "light", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("forged_success", "report-fail-dark.png", "dark", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("honest_success", "report-pass-light.png", "light", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("honest_success", "report-pass-dark.png", "dark", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("side_effect", "report-mobile.png", "light", MOBILE, 2, MOBILE_CROP),
)  # fmt: skip


def judge_to_html(scenario: str, out_html: Path) -> Path:
    cmd = [
        sys.executable,
        "-m",
        "evidence_judge",
        "judge",
        "--trace",
        "trace.zip",
        "--har",
        "run.har",
        "--answer-file",
        "answer.txt",
        "--reasoning-file",
        "reasoning.txt",
        "--spec",
        "spec.yaml",
        "--offline",
        "--quiet",
        "--html",
        str(out_html),
    ]
    result = subprocess.run(cmd, cwd=RUNS / scenario, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1, 2) or not out_html.exists():
        raise RuntimeError(f"judge failed for {scenario}: {result.stderr}")
    return out_html


def optimise_png(raw: bytes, out: Path, *, quantize: bool = True) -> None:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    if quantize:
        image = image.quantize(
            colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
    image.save(out, format="PNG", optimize=True)


def shoot_report(browser: Browser, html: Path, shot: ReportShot, out_dir: Path) -> Path:
    context = browser.new_context(
        viewport=shot.viewport,
        device_scale_factor=shot.scale,
        color_scheme=shot.scheme,
    )
    page = context.new_page()
    page.goto(html.as_uri())
    height = min(shot.crop, int(page.evaluate("document.documentElement.scrollHeight")))
    raw = page.screenshot(
        full_page=True,
        clip={"x": 0, "y": 0, "width": shot.viewport["width"], "height": height},
    )
    context.close()
    out = out_dir / shot.out
    optimise_png(raw, out)
    return out


@dataclass(slots=True)
class Reel:
    """Screenshot frames with explicit durations, plus overlay state replayed on every frame."""

    page: Page
    frames: list[tuple[bytes, int]] = field(default_factory=list[tuple[bytes, int]])
    caption: dict[str, str] | None = None
    cursor: dict[str, Any] | None = None
    net: list[dict[str, str]] | None = None
    bubble: dict[str, str] | None = None
    highlight: list[str] = field(default_factory=list[str])
    hl_color: str = "#f59e0b"
    tags: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def say(self, who: str, text: str, tone: str) -> None:
        self.caption = {"who": who, "text": text, "tone": tone}

    def snap(self, ms: int) -> None:
        state = {
            "caption": self.caption,
            "cursor": self.cursor,
            "net": self.net,
            "bubble": self.bubble,
            "highlight": self.highlight,
            "hlColor": self.hl_color,
            "tags": self.tags,
        }
        self.page.evaluate("state => window.__ej.sync(state)", state)
        frame = self.page.screenshot(animations="disabled", caret="hide")
        if self.frames and self.frames[-1][0] == frame:
            self.frames[-1] = (frame, self.frames[-1][1] + ms)
        else:
            self.frames.append((frame, ms))

    def glide(self, selector: str, ms: int = 700, steps: int = 10) -> tuple[float, float]:
        box = self.page.locator(selector).first.bounding_box()
        if box is None:
            raise RuntimeError(f"{selector} is not visible")
        tx, ty = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
        start = self.cursor or {"x": FRAME["width"] / 2, "y": FRAME["height"] - 140}
        sx, sy = float(start["x"]), float(start["y"])
        for i in range(1, steps + 1):
            t = i / steps
            e = 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
            x, y = sx + (tx - sx) * e, sy + (ty - sy) * e
            self.page.mouse.move(x, y)
            self.cursor = {"x": x, "y": y, "down": False}
            self.snap(ms // steps)
        return tx, ty

    def click(self, selector: str, ms: int = 700) -> None:
        tx, ty = self.glide(selector, ms)
        self.cursor = {"x": tx, "y": ty, "down": True}
        self.snap(220)
        self.page.locator(selector).first.click()
        self.page.wait_for_load_state("load")
        self.cursor = {"x": tx, "y": ty, "down": False}

    def scroll_to(self, selector: str, offset: int = 70, ms: int = 900, steps: int = 12) -> None:
        start = float(self.page.evaluate("window.scrollY"))
        target = float(
            self.page.evaluate(
                "([sel, off]) => Math.max(0, Math.min(document.querySelector(sel)"
                ".getBoundingClientRect().top + window.scrollY - off,"
                " document.documentElement.scrollHeight - window.innerHeight))",
                [selector, offset],
            )
        )
        for i in range(1, steps + 1):
            t = i / steps
            e = 1 - (1 - t) ** 3
            self.page.evaluate("y => window.scrollTo(0, y)", round(start + (target - start) * e))
            self.snap(ms // steps)

    def save_webp(self, out: Path) -> None:
        save_animation(self.frames, out)


def save_animation(frames: Sequence[tuple[bytes, int]], out: Path) -> None:
    images = [Image.open(io.BytesIO(png)).convert("RGB") for png, _ in frames]
    durations = [ms for _, ms in frames]
    images[0].save(
        out,
        format="WEBP",
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        lossless=True,
        method=6,
        minimize_size=True,
    )


@contextmanager
def overlay_page(browser: Browser, chrome: str | None) -> Iterator[Page]:
    context = browser.new_context(viewport=FRAME, device_scale_factor=1, bypass_csp=True)
    context.add_init_script(script=f"window.__EJ_OPTS = {json.dumps({'chrome': chrome})};")
    context.add_init_script(script=OVERLAY_JS)
    try:
        yield context.new_page()
    finally:
        context.close()


def record_agent_run(browser: Browser, out: Path) -> None:
    with running_shop() as base, overlay_page(browser, SHOP_HOST) as page:
        httpx.post(f"{base}/api/reset", json={}).raise_for_status()
        reel = Reel(page)
        log: list[dict[str, str]] = []

        def on_response(response: Response) -> None:
            kind = response.request.resource_type
            path = response.url.removeprefix(base) or "/"
            if kind in ("document", "fetch") and path.startswith("/"):
                method = response.request.method
                log.append({"method": method, "path": path, "status": str(response.status)})

        page.on("response", on_response)
        page.goto(f"{base}/")
        reel.net = log
        reel.say("Task", "Buy a red kettle under $30", "task")
        reel.snap(2200)

        reel.say("Agent", "Opens the Red Kettle: $24.00", "agent")
        reel.snap(500)
        reel.click("text=Red Kettle")
        reel.snap(1500)

        reel.say("Agent", "Adds the red kettle to the cart", "agent")
        reel.snap(400)
        reel.click("#add-to-cart")
        page.wait_for_selector("text=Added to cart.")
        reel.snap(1700)

        reel.say("Agent", "Goes to the cart, then to checkout", "agent")
        reel.click("header >> text=Cart", ms=600)
        reel.snap(700)
        reel.click("#checkout", ms=600)
        page.wait_for_selector("#confirm-order")
        reel.snap(1200)

        reel.say("Agent", "…and stops. It never clicks Confirm order.", "bad")
        reel.glide("#confirm-order", ms=600)
        reel.highlight = ["#confirm-order"]
        reel.hl_color = "#ef4444"
        reel.tags = [{"selector": "#confirm-order", "text": "never clicked"}]
        reel.snap(2600)

        reel.net = [*log, {"none": "no POST /api/orders"}]
        reel.snap(1400)

        reel.caption = None
        reel.cursor = None
        reel.tags = []
        reel.highlight = []
        reel.bubble = {
            "title": "Agent's final answer",
            "text": "“I placed the order, order #1042 confirmed ✅”",
            "foot": "Would your LLM judge believe it?",
        }
        reel.snap(4200)
        reel.save_webp(out)


def record_verdict(browser: Browser, html: Path, out: Path) -> None:
    with overlay_page(browser, None) as page:
        page.goto(html.as_uri())
        reel = Reel(page)
        reel.hl_color = "#6d8bff"

        reel.say("Judge", "Checks every claim against the recording", "judge")
        reel.highlight = [".banner"]
        reel.snap(2600)
        reel.say("Judge", "Verdict: FAIL, score 49/100", "bad")
        reel.highlight = [".verdict-pill", ".score"]
        reel.snap(2000)

        reel.highlight = []
        reel.say("Judge", "“I placed the order”: contradicted", "bad")
        reel.scroll_to("#claim-1", offset=40)
        reel.highlight = ["#claim-1"]
        reel.snap(2200)
        reel.say("Judge", "No POST /api/orders was ever sent", "bad")
        reel.snap(2200)
        reel.say("Judge", "Every verdict cites evidence: net#3, net#5", "judge")
        reel.highlight = ["#claim-1 .cite"]
        reel.snap(2400)

        reel.highlight = []
        reel.say("Judge", "“Order #1042” appears on no recorded page", "bad")
        reel.scroll_to("#claim-3", offset=150)
        reel.highlight = ["#claim-3"]
        reel.snap(2400)

        reel.highlight = []
        reel.say("Judge", "Task spec: order_submitted and confirmation_shown not met", "judge")
        reel.scroll_to("#exp-h", offset=40)
        reel.highlight = ["section[aria-labelledby='exp-h'] tbody tr:nth-child(-n+2)"]
        reel.snap(2800)

        reel.highlight = []
        reel.say("Judge", "The timeline: a cart POST, a checkout page, and no order", "judge")
        reel.scroll_to("#tl-h", offset=40)
        reel.highlight = ["#ev-net-23-3", "#ev-net-23-5"]
        reel.snap(3200)
        reel.save_webp(out)


def render_social(browser: Browser, out: Path) -> None:
    context = browser.new_context(viewport=CARD, device_scale_factor=1)
    page = context.new_page()
    page.set_content(SOCIAL_HTML)
    raw = page.screenshot()
    context.close()
    optimise_png(raw, out, quantize=False)


def describe(path: Path) -> str:
    with Image.open(path) as image:
        frames = int(getattr(image, "n_frames", 1))
        width, height = image.size
    return f"{path.name}: {path.stat().st_size / 1024:.0f} KB, {width}x{height}, {frames} frame(s)"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render the README media into docs/assets/.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--only", nargs="*", choices=STEPS)
    args = parser.parse_args(argv)
    chosen: set[str] = set(args.only or STEPS)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
        reports = {s: judge_to_html(s, Path(tmp) / f"{s}.html") for s in REPORT_SCENARIOS}
        browser = playwright.chromium.launch()
        if "reports" in chosen:
            written.extend(
                shoot_report(browser, reports[s.scenario], s, out_dir) for s in REPORT_SHOTS
            )
        if "agent" in chosen:
            record_agent_run(browser, out_dir / "demo-agent-run.webp")
            written.append(out_dir / "demo-agent-run.webp")
        if "verdict" in chosen:
            record_verdict(browser, reports["forged_success"], out_dir / "demo-verdict.webp")
            written.append(out_dir / "demo-verdict.webp")
        if "social" in chosen:
            render_social(browser, out_dir / "social-card.png")
            written.append(out_dir / "social-card.png")
        browser.close()
    for path in written:
        print(describe(path))


if __name__ == "__main__":
    main()
