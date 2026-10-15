"""Render the README media into docs/assets/: report screenshots, two demo animations and the
social preview card.

Reports come from the real CLI run offline on the recorded fixtures. The agent animation drives
the demo shop through the forged_success scenario with Playwright and composes the real shop
screenshots into an "agent console" capture page; the verdict animation steps the real HTML
report's CSS animations frame by frame. Capture pages (never the product report) load Space
Grotesk and JetBrains Mono from Google Fonts and fall back to system fonts offline. Every frame
is a screenshot with a fixed duration, so the output is crisp and the same on every run.

    uv run --with pillow python -m scripts.make_readme_media [--only reports agent verdict social]
"""

from __future__ import annotations

import argparse
import base64
import io
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image  # pyright: ignore[reportMissingImports]
from playwright.sync_api import Browser, Page, Response, ViewportSize, sync_playwright
from scripts.record_fixtures import TASK, running_shop

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "tests" / "fixtures" / "runs"
DEFAULT_OUT = ROOT / "docs" / "assets"

PNG_BUDGET = 400 * 1024
ANIMATION_BUDGET = 3 * 1024 * 1024
REPORT_WIDTH = 1200
REPORT_CROP = 1150
MOBILE: ViewportSize = {"width": 390, "height": 844}
MOBILE_CROP = 1590
STAGE: ViewportSize = {"width": 1120, "height": 760}
SHOP_VIEW: ViewportSize = {"width": 800, "height": 560}
SHOP_SCALE = 0.8
CARD: ViewportSize = {"width": 1280, "height": 640}
SHOP_HOST = "kettle.shop"

Scheme = Literal["light", "dark"]
Step = Literal["reports", "agent", "verdict", "social"]
STEPS: tuple[Step, ...] = ("reports", "agent", "verdict", "social")

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700'
    '&family=Space+Grotesk:wght@400;500;600;700&display=block">'
)

BASE_CSS = """
:root {
  --bg: #07080c; --bg-2: #0d1017; --panel: rgba(17, 20, 30, 0.82); --line: rgba(255, 255, 255, 0.08);
  --line-2: rgba(255, 255, 255, 0.14); --text: #eef0f7; --text-2: #b4bacb; --text-3: #7d8499;
  --violet: #8b5cf6; --violet-2: #a78bfa; --cyan: #22d3ee; --ok: #10b981; --bad: #f43f5e; --warn: #f59e0b;
  --display: "Space Grotesk", ui-sans-serif, system-ui, -apple-system, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; overflow: hidden; }
body {
  color: var(--text); font-family: var(--display); -webkit-font-smoothing: antialiased;
  background:
    radial-gradient(900px 420px at 8% -80px, rgba(139, 92, 246, 0.20), transparent 70%),
    radial-gradient(800px 420px at 100% 0%, rgba(34, 211, 238, 0.10), transparent 70%),
    linear-gradient(rgba(148, 163, 255, 0.05) 1px, transparent 1px) 0 0 / 28px 28px,
    linear-gradient(90deg, rgba(148, 163, 255, 0.05) 1px, transparent 1px) 0 0 / 28px 28px,
    linear-gradient(180deg, var(--bg), var(--bg-2));
}
.glass {
  position: relative; border-radius: 16px; border: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.05), transparent 90px), var(--panel);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 30px 60px -30px rgba(0, 0, 0, 0.9);
}
.lbl { font: 600 11px/1 var(--mono); letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-3); }
"""

CONSOLE_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">"""
    + FONTS
    + """<style>"""
    + BASE_CSS
    + """
html, body { width: 1120px; height: 760px; }
.top { position: absolute; left: 16px; right: 16px; top: 12px; height: 44px; display: flex; align-items: center;
  gap: 14px; padding: 0 16px; border-radius: 12px; font: 500 13px/1 var(--mono); color: var(--text-2); }
.live { display: inline-flex; align-items: center; gap: 8px; color: var(--text); font-weight: 700; letter-spacing: 0.12em; }
.live i { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); box-shadow: 0 0 0 var(--ring, 3px) rgba(244, 63, 94, 0.22), 0 0 12px var(--bad); }
.live.done i { background: var(--ok); box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.22), 0 0 12px var(--ok); }
.sep { color: var(--text-3); }
.top b { color: var(--text); font-weight: 600; }
.top .model { padding: 4px 8px; border-radius: 6px; border: 1px solid rgba(139, 92, 246, 0.4); background: rgba(139, 92, 246, 0.12); color: var(--violet-2); }
.top .right { margin-left: auto; display: flex; gap: 16px; align-items: center; }
.top .clock { color: var(--cyan); }
.browser { position: absolute; left: 16px; top: 70px; width: 658px; height: 500px; padding: 8px; overflow: hidden; }
.chrome { display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 6px 8px; }
.dots { display: flex; gap: 7px; }
.dots i { width: 11px; height: 11px; border-radius: 50%; display: block; background: #2a2f3c; }
.dots i:nth-child(1) { background: #ff5f57; } .dots i:nth-child(2) { background: #febc2e; } .dots i:nth-child(3) { background: #28c840; }
.url { flex: 1; display: flex; align-items: center; gap: 8px; height: 26px; padding: 0 12px; border-radius: 8px;
  background: rgba(255, 255, 255, 0.05); border: 1px solid var(--line); font: 12.5px/1 var(--mono); color: var(--text-2); }
.url .lock { color: var(--ok); }
.url b { color: var(--text); font-weight: 500; }
.ctl { font: 600 10px/1 var(--mono); letter-spacing: 0.14em; color: var(--violet-2); padding: 5px 8px; border-radius: 6px;
  background: rgba(139, 92, 246, 0.14); border: 1px solid rgba(139, 92, 246, 0.35); }
#vp { position: relative; width: 640px; height: 448px; border-radius: 10px; overflow: hidden; background: #faf7f2; }
#vp img { position: absolute; inset: 0; width: 640px; height: 448px; display: none; }
#vp img.on { display: block; }
.cursor { position: absolute; width: 24px; height: 24px; z-index: 5; filter: drop-shadow(0 3px 4px rgba(0, 0, 0, 0.45)); }
.ripple { position: absolute; z-index: 4; border-radius: 50%; border: 2px solid var(--violet); background: rgba(139, 92, 246, 0.18); }
.marker { position: absolute; z-index: 3; border-radius: 999px; border: 2px dashed var(--bad); box-shadow: 0 0 0 6px rgba(244, 63, 94, 0.15), 0 0 24px rgba(244, 63, 94, 0.5); }
.marker span { position: absolute; left: calc(100% + 14px); top: 50%; transform: translateY(-50%); white-space: nowrap;
  padding: 6px 10px; border-radius: 8px; background: var(--bad); color: #fff; font: 700 12px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase;
  box-shadow: 0 8px 20px -6px rgba(244, 63, 94, 0.8); }
.marker span::before { content: ""; position: absolute; left: -6px; top: 50%; margin-top: -6px; border: 6px solid transparent; border-left: 0; border-right-color: var(--bad); }
.thoughts { position: absolute; left: 688px; top: 70px; width: 416px; height: 500px; padding: 16px 16px 14px; overflow: hidden; }
.thoughts header, .net header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.thoughts header .lbl, .net header .lbl { color: var(--violet-2); }
.thoughts header .lbl::before, .net header .lbl::before { content: "\\25C6  "; color: var(--cyan); }
.tick { font: 500 11px/1 var(--mono); color: var(--text-3); }
.thought { position: relative; margin-bottom: 10px; padding: 11px 12px 12px 14px; border-radius: 12px; border: 1px solid var(--line); background: rgba(255, 255, 255, 0.025); }
.thought::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 2px; border-radius: 2px; background: var(--violet); box-shadow: 0 0 10px var(--violet); }
.thought.old { opacity: 0.6; }
.thought.old::before { background: var(--text-3); box-shadow: none; }
.thought .lbl { display: block; margin-bottom: 7px; font-size: 10px; }
.thought p { margin: 0; font: 500 15.5px/1.45 var(--display); color: var(--text); letter-spacing: -0.005em; }
#th { height: 268px; overflow: hidden; }
.acts { position: absolute; left: 16px; right: 16px; bottom: 14px; height: 176px; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line); background: rgba(0, 0, 0, 0.25); overflow: hidden; }
.acts .lbl { display: block; margin-bottom: 10px; color: var(--cyan); }
.act { display: flex; align-items: center; gap: 8px; height: 22px; font: 500 12.5px/1 var(--mono); color: var(--text-2); white-space: nowrap; }
.act::before { content: "\\203A"; color: var(--violet-2); font-weight: 700; }
.act b { color: var(--text); font-weight: 500; }
.act em { margin-left: auto; font-style: normal; color: var(--ok); }
.act.ghost, .act.ghost b, .act.ghost em { color: var(--bad); }
.act.ghost b { text-decoration: line-through; text-decoration-color: rgba(244, 63, 94, 0.6); }
.caret { display: inline-block; width: 8px; height: 17px; margin-left: 2px; vertical-align: -3px; background: var(--cyan); box-shadow: 0 0 8px var(--cyan); }
.net { position: absolute; left: 16px; right: 16px; top: 584px; height: 162px; padding: 12px 16px; overflow: hidden; }
.net header { margin-bottom: 8px; }
.row { display: grid; grid-template-columns: 54px 52px 230px 96px 1fr; align-items: center; gap: 10px; height: 20px; font: 500 12.5px/1 var(--mono); color: var(--text-2); }
.row .id { color: var(--cyan); }
.row .m { color: var(--text-3); font-weight: 700; }
.row .s { color: var(--ok); }
.row .lane { position: relative; height: 6px; border-radius: 3px; background: rgba(255, 255, 255, 0.05); }
.row .lane i { position: absolute; top: 0; bottom: 0; border-radius: 3px; background: rgba(34, 211, 238, 0.7); }
.row.hl { color: var(--text); }
.row.hl .m { color: var(--warn); }
.row.hl .lane i { background: var(--warn); box-shadow: 0 0 10px var(--warn); }
.row.hl .p::after { content: "  state change"; color: var(--warn); font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; }
.row.ghost { color: var(--bad); }
.row.ghost .id, .row.ghost .m, .row.ghost .s { color: var(--bad); }
.row.ghost .lane { background: repeating-linear-gradient(90deg, rgba(244, 63, 94, 0.5) 0 6px, transparent 6px 12px); }
.row.ghost .p { text-decoration: line-through; text-decoration-color: rgba(244, 63, 94, 0.6); }
.final { position: absolute; inset: 0; z-index: 20; display: grid; place-items: center; background: rgba(5, 6, 10, 0.72); backdrop-filter: blur(5px); }
.say { position: relative; width: 760px; padding: 30px 36px 32px; border-radius: 24px; border: 1px solid rgba(139, 92, 246, 0.35);
  background: linear-gradient(180deg, rgba(139, 92, 246, 0.16), rgba(139, 92, 246, 0) 70%), #11141e; box-shadow: 0 40px 80px -30px rgba(0, 0, 0, 0.9), 0 0 60px -20px rgba(139, 92, 246, 0.6); }
.say .who { display: flex; align-items: center; gap: 12px; }
.say .av { width: 38px; height: 38px; border-radius: 11px; display: grid; place-items: center; background: linear-gradient(135deg, var(--violet), var(--cyan)); font-size: 20px; }
.say .who .lbl { color: var(--violet-2); }
.say q { display: block; margin: 18px 0 0; font: 700 38px/1.18 var(--display); letter-spacing: -0.03em; color: #fff; quotes: "\\201C" "\\201D"; }
.say .meta { margin-top: 18px; display: flex; gap: 10px; flex-wrap: wrap; }
.say .meta span { padding: 6px 10px; border-radius: 8px; border: 1px solid var(--line-2); font: 500 12px/1 var(--mono); color: var(--text-2); }
.say .meta span.ok { color: var(--ok); border-color: rgba(16, 185, 129, 0.4); }
.ask { margin-top: 26px; text-align: center; font: 700 26px/1.2 var(--display); letter-spacing: -0.02em; color: var(--warn); }
.ask span { display: inline-block; padding: 12px 22px; border-radius: 999px; border: 1px solid rgba(245, 158, 11, 0.5); background: rgba(245, 158, 11, 0.1); }
</style></head><body>
<div class="top glass">
  <span class="live" id="live"><i></i><span id="livetext">AGENT RUN</span></span>
  <span class="sep">&middot;</span><span>task: <b>"""
    + TASK
    + """</b></span>
  <span class="sep">&middot;</span><span>model: <span class="model">generic agent</span></span>
  <span class="right"><span id="step">step 0/6</span><span class="clock" id="clock">00:00.0</span></span>
</div>
<div class="browser glass">
  <div class="chrome"><span class="dots"><i></i><i></i><i></i></span>
    <span class="url"><span class="lock">&#9679;</span><span id="url"></span></span><span class="ctl">AGENT CONTROL</span></div>
  <div id="vp"></div>
</div>
<div class="thoughts glass"><header><span class="lbl">Agent thoughts</span><span class="tick" id="tok">streaming</span></header><div id="th"></div><div class="acts"><span class="lbl">Tool calls</span><div id="acts"></div></div></div>
<div class="net glass"><header><span class="lbl">Network &middot; live capture</span><span class="tick" id="ncount"></span></header><div id="rows"></div></div>
<div id="final"></div>
<script>
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const S = """
    + str(SHOP_SCALE)
    + """;
const CURSOR = '<svg viewBox="0 0 24 24" width="24" height="24"><path d="M4 2 L4 20 L8.6 15.8 L11.8 22.6 L15 21.2 L11.9 14.4 L18 14.4 Z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
window.init = async (shots) => {
  const vp = $("#vp");
  for (const src of shots) { const img = new Image(); img.src = src; vp.appendChild(img); }
  await Promise.all([...vp.querySelectorAll("img")].map((i) => i.decode()));
  await document.fonts.ready;
};
window.render = (st) => {
  const imgs = document.querySelectorAll("#vp img");
  imgs.forEach((img, i) => img.classList.toggle("on", i === st.shot));
  $("#url").innerHTML = "";
  $("#url").append(el("b", "", st.host), st.path);
  $("#step").textContent = `step ${st.step}/6`;
  $("#clock").textContent = st.clock;
  $("#live").classList.toggle("done", st.done);
  $("#livetext").textContent = st.done ? "RUN FINISHED" : "AGENT RUN";
  $("#live").style.setProperty("--ring", `${3 + 3 * st.pulse}px`);
  $("#tok").textContent = st.done ? "done" : "streaming";
  const vp = $("#vp");
  vp.querySelectorAll(".cursor, .ripple, .marker").forEach((e) => e.remove());
  if (st.marker) {
    const m = el("div", "marker");
    const pad = 6;
    Object.assign(m.style, { left: `${st.marker.x * S - pad}px`, top: `${st.marker.y * S - pad}px`, width: `${st.marker.w * S + 2 * pad}px`, height: `${st.marker.h * S + 2 * pad}px` });
    m.append(el("span", "", st.marker.label));
    vp.append(m);
  }
  if (st.ripple) {
    const r = el("div", "ripple");
    const size = 14 + 46 * st.ripple.t;
    Object.assign(r.style, { left: `${st.ripple.x * S - size / 2}px`, top: `${st.ripple.y * S - size / 2}px`, width: `${size}px`, height: `${size}px`, opacity: `${1 - st.ripple.t * 0.8}` });
    vp.append(r);
  }
  if (st.cursor) {
    const c = el("div", "cursor");
    c.innerHTML = CURSOR;
    Object.assign(c.style, { left: `${st.cursor.x * S - 4}px`, top: `${st.cursor.y * S - 2}px`, transform: st.cursor.down ? "scale(0.86)" : "none" });
    vp.append(c);
  }
  const th = $("#th");
  th.innerHTML = "";
  st.thoughts.forEach((t, i) => {
    const box = el("div", "thought" + (i < st.thoughts.length - 1 ? " old" : ""));
    box.append(el("span", "lbl", `step ${t.n} · reasoning`));
    const p = el("p", "", t.text.slice(0, t.shown));
    if (t.shown < t.text.length || (i === st.thoughts.length - 1 && !st.done && st.caret)) p.append(el("span", "caret"));
    box.append(p);
    th.append(box);
  });
  while (th.scrollHeight > th.clientHeight + 1 && th.firstChild && th.childElementCount > 1) th.firstChild.remove();
  const acts = $("#acts");
  acts.innerHTML = "";
  st.actions.slice(-6).forEach((a) => {
    const row = el("div", "act" + (a.ghost ? " ghost" : ""));
    const call = el("span"); call.append(a.name + "(", el("b", "", a.arg), ")"); row.append(call, el("em", "", a.result));
    acts.append(row);
  });
  const rows = $("#rows");
  rows.innerHTML = "";
  st.net.forEach((r) => {
    const row = el("div", "row" + (r.hl ? " hl" : "") + (r.ghost ? " ghost" : ""));
    row.append(el("span", "id", r.id), el("span", "m", r.m), el("span", "p", r.p), el("span", "s", r.s));
    const lane = el("span", "lane");
    if (!r.ghost) { const bar = el("i"); bar.style.left = `${((r.at - 1) / 6) * 90 + 2}%`; bar.style.width = `${Math.max(1.2, 6 * r.grow)}%`; lane.append(bar); }
    row.append(lane);
    rows.append(row);
  });
  const nreq = st.net.filter((r) => !r.ghost).length, nmut = st.net.filter((r) => r.m === "POST" && !r.ghost).length;
  $("#ncount").textContent = `${nreq} request${nreq === 1 ? "" : "s"} · ${nmut} state change${nmut === 1 ? "" : "s"}`;
  const fin = $("#final");
  fin.innerHTML = "";
  if (st.final) {
    const wrap = el("div", "final");
    wrap.style.opacity = st.final.fade;
    const say = el("div", "say");
    const who = el("div", "who");
    who.append(el("span", "av", "\\u{1F916}"), el("span", "lbl", "Agent · final answer"));
    const meta = el("div", "meta");
    meta.append(el("span", "ok", "status: success"), el("span", "", "steps: 6"), el("span", "", "self-reported"));
    say.append(who, el("q", "", st.final.text), meta);
    const ask = el("div", "ask");
    const a = el("span", "", "Would your LLM judge believe it?");
    a.style.boxShadow = `0 0 ${10 + 30 * st.final.pulse}px rgba(245, 158, 11, ${0.15 + 0.4 * st.final.pulse})`;
    a.style.transform = `scale(${1 + 0.03 * st.final.pulse})`;
    a.style.opacity = st.final.ask;
    ask.append(a);
    const col = el("div");
    col.append(say, ask);
    wrap.append(col);
    fin.append(wrap);
  }
};
</script></body></html>"""
)

JUDGE_OVERLAY_CSS = """
body { padding-top: 58px !important; }
.wrap { max-width: 1100px !important; }
.ej-bar { position: fixed; z-index: 50; left: 0; right: 0; top: 0; height: 52px; display: flex; align-items: center; gap: 14px;
  padding: 0 22px; background: rgba(7, 8, 12, 0.88); backdrop-filter: blur(10px); border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  font: 500 13px/1 "JetBrains Mono", ui-monospace, monospace; color: #b4bacb; }
.ej-bar .who { display: inline-flex; align-items: center; gap: 9px; font-weight: 700; letter-spacing: 0.14em; color: #eef0f7; }
.ej-bar .who i { width: 9px; height: 9px; border-radius: 50%; background: #22d3ee; box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.2), 0 0 12px #22d3ee; }
.ej-bar .cmd { color: #7d8499; }
.ej-bar .cmd b { color: #a78bfa; font-weight: 500; }
.ej-bar .st { margin-left: auto; padding: 6px 10px; border-radius: 8px; border: 1px solid rgba(139, 92, 246, 0.4); background: rgba(139, 92, 246, 0.12); color: #c4b5fd; }
.ej-pending { --t: #8b5cf6 !important; }
.ej-pending .claim-badges .badge { font-size: 0 !important; }
.ej-pending .claim-badges .badge::after { content: "checking"; font-size: 12px; }
.ej-pending .evidence { opacity: 0.28; }
.ej-pending::before { opacity: 0.6; }
.ej-flash { box-shadow: 0 0 0 2px rgba(244, 63, 94, 0.9), 0 0 50px -6px rgba(244, 63, 94, 0.75) !important; }
.ej-flash-ok { box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.9), 0 0 50px -6px rgba(16, 185, 129, 0.7) !important; }
.ej-lit { border-color: #22d3ee !important; box-shadow: 0 0 0 1px #22d3ee, 0 0 22px -2px rgba(34, 211, 238, 0.9) !important; }
.ej-tag { position: fixed; z-index: 60; max-width: 360px; padding: 12px 16px 13px; border-radius: 12px; color: #fff;
  font: 600 16px/1.3 "Space Grotesk", ui-sans-serif, system-ui, sans-serif; letter-spacing: -0.01em;
  background: linear-gradient(180deg, rgba(244, 63, 94, 0.95), rgba(190, 18, 60, 0.95)); box-shadow: 0 18px 40px -12px rgba(244, 63, 94, 0.8); }
.ej-tag.cyan { background: linear-gradient(180deg, rgba(34, 211, 238, 0.95), rgba(8, 145, 178, 0.95)); color: #04121a; box-shadow: 0 18px 40px -12px rgba(34, 211, 238, 0.7); }
.ej-tag small { display: block; margin-bottom: 5px; font: 700 10.5px/1 "JetBrains Mono", ui-monospace, monospace; letter-spacing: 0.14em; text-transform: uppercase; opacity: 0.8; }
.ej-tag::before { content: ""; position: absolute; top: 50%; margin-top: -8px; border: 8px solid transparent; }
.ej-tag.left::before { right: -14px; border-left: 8px solid rgba(220, 38, 78, 0.95); border-right: 0; }
.ej-tag.cyan.left::before { border-left-color: rgba(34, 211, 238, 0.95); }
.ej-ghost { border: 1px dashed rgba(244, 63, 94, 0.7); border-radius: 10px; margin: 6px 10px 4px; background: rgba(244, 63, 94, 0.08) !important; }
.ej-ghost .label { color: #f43f5e !important; text-decoration: line-through; text-decoration-color: rgba(244, 63, 94, 0.6); }
.ej-ghost .node { border-color: #f43f5e !important; border-style: dashed !important; background: transparent !important; }
.ej-ghost .badge { --t: #f43f5e; }
"""

JUDGE_OVERLAY_JS = r"""
(css) => {
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700&family=Space+Grotesk:wght@600;700&display=block";
  document.head.appendChild(link);
  const bar = document.createElement("div");
  bar.className = "ej-bar";
  bar.innerHTML = '<span class="who"><i></i>THE JUDGE</span><span class="cmd">$ evidence-judge judge <b>--trace</b> trace.zip <b>--har</b> run.har <b>--spec</b> spec.yaml</span><span class="st" id="ej-st"></span>';
  document.body.appendChild(bar);
  const ghost = document.createElement("li");
  ghost.className = "ej-ghost";
  ghost.hidden = true;
  ghost.innerHTML = '<span class="time">never</span><span class="node"></span><span class="what"><span class="kind">net#?</span><span class="label">POST /api/orders</span><span class="detail">no order request anywhere in the recording</span></span><span class="lane opt"></span><span class="badge">missing</span>';
  document.querySelector(".trace ol").appendChild(ghost);
  window.__ej = {
    time(ms) { document.getAnimations().forEach((a) => { a.pause(); a.currentTime = ms; }); },
    sync(st) {
      document.getElementById("ej-st").textContent = st.status;
      document.querySelectorAll(".claim").forEach((c, i) => {
        c.classList.toggle("ej-pending", i >= st.revealed);
        c.classList.toggle("ej-flash", i === st.flash && c.classList.contains("t-bad"));
        c.classList.toggle("ej-flash-ok", i === st.flash && c.classList.contains("t-ok"));
      });
      document.querySelectorAll(".ej-lit").forEach((e) => e.classList.remove("ej-lit"));
      for (const sel of st.lit) document.querySelectorAll(sel).forEach((e) => e.classList.add("ej-lit"));
      ghost.hidden = !st.ghost;
      document.querySelectorAll(".ej-tag").forEach((e) => e.remove());
      for (const tag of st.tags) {
        const target = document.querySelector(tag.sel);
        if (!target) continue;
        const box = target.getBoundingClientRect();
        const t = document.createElement("div");
        t.className = "ej-tag left " + (tag.tone || "");
        const small = document.createElement("small");
        small.textContent = tag.kicker;
        t.append(small, tag.text);
        document.body.appendChild(t);
        const w = t.getBoundingClientRect().width, h = t.getBoundingClientRect().height;
        if (tag.mode === "in") {
          t.style.left = `${box.right - w - 14}px`;
          t.style.top = `${box.bottom - h - 14}px`;
        } else {
          t.style.left = `${Math.max(12, box.left - w - 18 + (tag.dx || 0))}px`;
          t.style.top = `${box.top + box.height / 2 - h / 2 + (tag.dy || 0)}px`;
        }
        t.style.opacity = tag.alpha ?? 1;
      }
    },
  };
}
"""

SOCIAL_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">"""
    + FONTS
    + """<style>"""
    + BASE_CSS
    + """
html, body { width: 1280px; height: 640px; }
body { display: grid; grid-template-columns: 1fr 560px; gap: 40px; padding: 64px 64px 56px 72px; }
.brand { display: flex; align-items: center; gap: 12px; font: 600 15px/1 var(--mono); letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-2); }
.mark { width: 40px; height: 40px; border-radius: 11px; display: grid; place-items: center; background: linear-gradient(135deg, var(--violet), var(--cyan));
  color: #07080c; font: 800 16px/1 var(--mono); letter-spacing: -0.02em; box-shadow: 0 0 30px -6px var(--violet); }
h1 { margin: 44px 0 0; font: 700 84px/0.95 var(--display); letter-spacing: -0.05em; }
h1 span { background: linear-gradient(90deg, #c4b5fd, var(--cyan)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.tag { margin: 26px 0 0; font: 500 30px/1.25 var(--display); letter-spacing: -0.02em; color: var(--text-2); max-width: 560px; }
.tag b { color: var(--text); font-weight: 700; }
.chips { margin-top: 38px; display: flex; flex-wrap: wrap; gap: 10px; }
.chips span { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--line-2); background: rgba(255, 255, 255, 0.03); font: 500 14px/1 var(--mono); color: var(--text-2); }
.chips span::before { content: "\\25C6  "; color: var(--cyan); font-size: 10px; }
.card { align-self: center; padding: 24px 24px 22px; }
.card .lbl { display: block; margin-bottom: 10px; }
.said { padding: 14px 16px; border-radius: 12px; border: 1px solid rgba(245, 158, 11, 0.3); background: rgba(245, 158, 11, 0.07); }
.said .lbl { color: var(--warn); }
.said p { margin: 0; font: 700 25px/1.25 var(--display); letter-spacing: -0.02em; }
.ev { margin-top: 14px; }
.ev .lbl { color: var(--cyan); }
.ev ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }
.ev li { display: grid; grid-template-columns: 62px 1fr auto; gap: 12px; align-items: center; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.025); font: 500 15px/1 var(--mono); color: var(--text-2); }
.ev .id { color: var(--cyan); }
.ev .s { color: var(--ok); }
.ev li.miss { border: 1px dashed rgba(244, 63, 94, 0.7); background: rgba(244, 63, 94, 0.08); color: #fecdd3; }
.ev li.miss .id, .ev li.miss .s { color: var(--bad); }
.stamp { position: absolute; right: -22px; top: -30px; transform: rotate(8deg); padding: 8px 20px 9px; border: 4px solid var(--bad); border-radius: 14px;
  color: var(--bad); font: 800 52px/1 var(--display); letter-spacing: 0.1em; background: rgba(7, 8, 12, 0.9); outline: 1px solid rgba(244, 63, 94, 0.45); outline-offset: 5px;
  box-shadow: 0 0 40px -6px rgba(244, 63, 94, 0.8); text-shadow: 0 0 20px rgba(244, 63, 94, 0.6); }
</style></head><body>
<div>
  <div class="brand"><span class="mark">EJ</span>evidence-judge</div>
  <h1>Evidence<br><span>Judge</span></h1>
  <p class="tag"><b>Don't trust what the agent says.</b> Check what it did.</p>
  <div class="chips"><span>Playwright traces</span><span>HAR</span><span>browser-use</span><span>offline</span></div>
</div>
<div class="card glass">
  <div class="stamp">FAIL</div>
  <div class="said"><span class="lbl">Agent said</span><p>&ldquo;I placed the order, order #1042 confirmed.&rdquo;</p></div>
  <div class="ev"><span class="lbl">Evidence</span><ul>
    <li><span class="id">net#3</span>POST /api/cart<span class="s">200</span></li>
    <li><span class="id">net#5</span>GET /checkout<span class="s">200</span></li>
    <li class="miss"><span class="id">net#?</span>POST /api/orders<span class="s">never sent</span></li>
    <li><span class="id">final</span>/checkout<span class="s" style="color: var(--bad)">no #1042</span></li>
  </ul></div>
</div>
</body></html>"""
)


@dataclass(frozen=True, slots=True)
class ReportShot:
    scenario: str
    out: str
    scheme: Scheme
    viewport: ViewportSize
    scale: int
    crop: int


REPORT_SCENARIOS = ("forged_success", "honest_success")
REPORT_SHOTS: tuple[ReportShot, ...] = (
    ReportShot("forged_success", "report-fail-dark.png", "dark", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("forged_success", "report-fail-light.png", "light", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("honest_success", "report-pass-dark.png", "dark", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("honest_success", "report-pass-light.png", "light", {"width": REPORT_WIDTH, "height": 900}, 1, REPORT_CROP),
    ReportShot("forged_success", "report-mobile.png", "dark", MOBILE, 2, MOBILE_CROP),
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


def optimise_png(raw: bytes, out: Path) -> None:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    if buffer.tell() > PNG_BUDGET:
        palette = image.quantize(
            colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
        image = image.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
    image.save(out, format="PNG", optimize=True)


CLEAN_CUT_JS = """
(limit) => {
  let cut = 0;
  for (const el of document.querySelectorAll(".banner, .dims, .claim, section > header")) {
    const r = el.getBoundingClientRect();
    const bottom = r.bottom + window.scrollY;
    if (bottom <= limit - 6 && bottom > cut) cut = bottom;
  }
  return Math.ceil(cut + 6);
}
"""


def shoot_report(browser: Browser, html: Path, shot: ReportShot, out_dir: Path) -> Path:
    context = browser.new_context(
        viewport=shot.viewport,
        device_scale_factor=shot.scale,
        color_scheme=shot.scheme,
    )
    page = context.new_page()
    page.goto(html.as_uri())
    height = int(page.evaluate(CLEAN_CUT_JS, shot.crop))
    raw = page.screenshot(
        full_page=True,
        animations="disabled",
        clip={"x": 0, "y": 0, "width": shot.viewport["width"], "height": height},
    )
    context.close()
    out = out_dir / shot.out
    optimise_png(raw, out)
    return out


@dataclass(slots=True)
class Film:
    """Screenshot frames with explicit durations; identical consecutive frames are merged."""

    page: Page
    animations: Literal["disabled", "allow"] = "disabled"
    frames: list[tuple[bytes, int]] = field(default_factory=list[tuple[bytes, int]])

    def snap(self, ms: int) -> None:
        frame = self.page.screenshot(animations=self.animations, caret="hide")
        if self.frames and self.frames[-1][0] == frame:
            self.frames[-1] = (frame, self.frames[-1][1] + ms)
        else:
            self.frames.append((frame, ms))

    @property
    def elapsed(self) -> int:
        return sum(ms for _, ms in self.frames)


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
        quality=100,
        method=6,
        minimize_size=True,
    )
    if out.stat().st_size > ANIMATION_BUDGET:
        images[0].save(
            out,
            format="WEBP",
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
            lossless=False,
            quality=92,
            method=6,
            allow_mixed=True,
        )


def ease(t: float) -> float:
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@dataclass(frozen=True, slots=True)
class ShopFrame:
    png: bytes
    path: str


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x + min(self.w / 2, 70), self.y + self.h / 2)


@dataclass(slots=True)
class AgentRun:
    """What the scripted agent did in the shop: screenshots, click targets and the network log."""

    shots: list[ShopFrame] = field(default_factory=list[ShopFrame])
    targets: dict[str, Box] = field(default_factory=dict[str, Box])
    log: list[dict[str, str]] = field(default_factory=list[dict[str, str]])


def drive_shop(browser: Browser) -> AgentRun:
    run = AgentRun()
    with running_shop() as base:
        httpx.post(f"{base}/api/reset", json={}).raise_for_status()
        context = browser.new_context(viewport=SHOP_VIEW, device_scale_factor=2)
        page = context.new_page()

        def on_response(response: Response) -> None:
            kind = response.request.resource_type
            path = response.url.removeprefix(base) or "/"
            if kind in ("document", "fetch") and path.startswith("/"):
                run.log.append({"m": response.request.method, "p": path, "s": str(response.status)})

        def shoot() -> None:
            page.wait_for_timeout(120)
            path = page.url.removeprefix(base) or "/"
            run.shots.append(ShopFrame(page.screenshot(animations="disabled"), path))

        def target(name: str, selector: str) -> None:
            box = page.locator(selector).first.bounding_box()
            if box is None:
                raise RuntimeError(f"{selector} is not visible")
            run.targets[name] = Box(box["x"], box["y"], box["width"], box["height"])

        page.on("response", on_response)
        page.goto(f"{base}/")
        shoot()
        target("product", "text=Red Kettle")
        page.click("text=Red Kettle")
        page.wait_for_selector("#add-to-cart")
        shoot()
        target("add", "#add-to-cart")
        with page.expect_response("**/api/cart"):
            page.click("#add-to-cart")
        page.wait_for_selector("text=Added to cart.")
        shoot()
        target("cart", "header >> text=Cart")
        page.click("header >> text=Cart")
        page.wait_for_selector("#checkout")
        shoot()
        target("checkout", "#checkout")
        page.click("#checkout")
        page.wait_for_selector("#confirm-order")
        shoot()
        target("confirm", "#confirm-order")
        context.close()
    return run


@dataclass(slots=True)
class Console:
    """Overlay state for the agent console capture page, replayed on every frame."""

    film: Film
    run: AgentRun
    shot: int = 0
    step: int = 1
    cursor: tuple[float, float] = (420.0, 470.0)
    down: bool = False
    ripple: dict[str, float] | None = None
    marker: dict[str, Any] | None = None
    thoughts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    net: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    actions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    done: bool = False
    final: dict[str, Any] | None = None
    blink: int = 0

    def snap(self, ms: int, pulse: float = 0.0) -> None:
        self.blink += 1
        elapsed = self.film.elapsed / 1000 * 1.9
        state = {
            "shot": self.shot,
            "host": SHOP_HOST,
            "path": self.run.shots[self.shot].path,
            "step": self.step,
            "clock": f"00:{int(elapsed):02d}.{int(elapsed * 10) % 10}",
            "done": self.done,
            "pulse": pulse,
            "caret": self.blink % 2 == 0,
            "cursor": None
            if self.done
            else {"x": self.cursor[0], "y": self.cursor[1], "down": self.down},
            "ripple": self.ripple,
            "marker": self.marker,
            "thoughts": self.thoughts,
            "net": self.net,
            "actions": self.actions,
            "final": self.final,
        }
        self.film.page.evaluate("st => window.render(st)", state)
        self.film.snap(ms)

    def think(self, n: int, text: str, ms: int = 1100) -> None:
        self.thoughts.append({"n": n, "text": text, "shown": 0})
        steps = max(8, min(22, len(text) // 4))
        for i in range(1, steps + 1):
            self.thoughts[-1]["shown"] = round(len(text) * i / steps)
            self.snap(ms // steps)

    def request(self, row: dict[str, str], *, hl: bool = False) -> None:
        index = len([r for r in self.net if not r.get("ghost")]) + 1
        self.net.append({"id": f"net#{index}", **row, "hl": hl, "at": index, "grow": 0.3})
        for grow in (0.65, 1.0):
            self.net[-1]["grow"] = grow
            self.snap(60)

    def glide(self, name: str, ms: int = 650, steps: int = 11) -> None:
        tx, ty = self.run.targets[name].centre
        sx, sy = self.cursor
        for i in range(1, steps + 1):
            e = ease(i / steps)
            self.cursor = (sx + (tx - sx) * e, sy + (ty - sy) * e)
            self.snap(ms // steps)

    def click(self, name: str, selector: str, then: Callable[[], None]) -> None:
        self.glide(name)
        self.actions.append({"name": "click", "arg": selector, "result": "ok"})
        x, y = self.cursor
        self.down = True
        for t in (0.0, 0.35, 0.7):
            self.ripple = {"x": x, "y": y, "t": t}
            self.snap(70)
        self.down = False
        self.ripple = {"x": x, "y": y, "t": 1.0}
        then()
        self.snap(80)
        self.ripple = None


def log_row(run: AgentRun, method: str, path: str) -> dict[str, str]:
    for row in run.log:
        if row["m"] == method and row["p"] == path:
            return row
    raise RuntimeError(f"{method} {path} was not recorded")


@contextmanager
def stage(browser: Browser, html: str) -> Iterator[Page]:
    context = browser.new_context(viewport=STAGE, device_scale_factor=1)
    try:
        page = context.new_page()
        page.set_content(html, wait_until="networkidle")
        yield page
    finally:
        context.close()


def record_agent_run(browser: Browser, out: Path) -> None:
    run = drive_shop(browser)
    answer = (RUNS / "forged_success" / "answer.txt").read_text(encoding="utf-8").strip()
    reasoning = [
        p.strip()
        for p in (RUNS / "forged_success" / "reasoning.txt")
        .read_text(encoding="utf-8")
        .split("\n\n")
        if p.strip()
    ]
    final_text = answer.split(". ")[0] + " ✅"
    with stage(browser, CONSOLE_HTML) as page:
        page.evaluate("shots => window.init(shots)", [data_uri(s.png) for s in run.shots])
        c = Console(Film(page), run)

        c.actions.append({"name": "goto", "arg": "/", "result": "200"})
        c.request(log_row(run, "GET", "/"))
        c.snap(500)
        c.think(1, reasoning[0], ms=1200)
        c.snap(700)

        def go(shot: int, method: str, path: str, *, hl: bool = False) -> Callable[[], None]:
            def apply() -> None:
                c.shot = shot
                c.request(log_row(run, method, path), hl=hl)

            return apply

        c.click("product", "text=Red Kettle", go(1, "GET", "/products/red-kettle"))
        c.snap(500)
        c.step = 2
        c.think(2, reasoning[1], ms=1100)
        c.click("add", "#add-to-cart", go(2, "POST", "/api/cart", hl=True))
        c.snap(1100)
        c.step = 3
        c.click("cart", "header >> text=Cart", go(3, "GET", "/cart"))
        c.snap(450)
        c.step = 4
        c.click("checkout", "#checkout", go(4, "GET", "/checkout"))
        c.snap(600)

        c.step = 5
        c.glide("confirm", ms=700)
        box = run.targets["confirm"]
        c.cursor = (box.x + box.w + 26, box.y + box.h + 30)
        c.marker = {"x": box.x, "y": box.y, "w": box.w, "h": box.h, "label": "never clicked"}
        c.snap(700)
        c.think(3, reasoning[2], ms=1500)
        c.net.append(
            {
                "id": "net#?",
                "m": "POST",
                "p": "/api/orders",
                "s": "never sent",
                "ghost": True,
                "at": 0,
                "grow": 0,
            }
        )
        c.actions.append(
            {"name": "click", "arg": "#confirm-order", "result": "never called", "ghost": True}
        )
        c.snap(1500)
        c.step = 6
        c.think(4, reasoning[3], ms=900)
        c.snap(900)

        c.done = True
        c.snap(500)
        for fade in (0.25, 0.55, 0.8, 1.0):
            c.final = {"text": final_text, "fade": fade, "pulse": 0.0, "ask": 0.0}
            c.snap(70)
        c.snap(1200)
        for i in range(10):
            pulse = (i % 4) / 3 if i % 4 < 3 else 0.33
            c.final = {
                "text": final_text,
                "fade": 1.0,
                "pulse": pulse,
                "ask": min(1.0, (i + 1) / 3),
            }
            c.snap(260)
        c.snap(1400)
        save_animation(c.film.frames, out)


@dataclass(slots=True)
class Judge:
    """Overlay state for the report, replayed on every frame."""

    film: Film
    status: str = "reading trace.zip + run.har"
    revealed: int = 0
    flash: int = -1
    lit: list[str] = field(default_factory=list[str])
    ghost: bool = False
    tags: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def snap(self, ms: int) -> None:
        state = {
            "status": self.status,
            "revealed": self.revealed,
            "flash": self.flash,
            "lit": self.lit,
            "ghost": self.ghost,
            "tags": self.tags,
        }
        self.film.page.evaluate("st => window.__ej.sync(st)", state)
        self.film.snap(ms)

    def scroll_to(self, selector: str, offset: int, ms: int = 900, steps: int = 10) -> None:
        page = self.film.page
        start = float(page.evaluate("window.scrollY"))
        target = float(
            page.evaluate(
                "([sel, off]) => Math.max(0, Math.min(document.querySelector(sel)"
                ".getBoundingClientRect().top + window.scrollY - off,"
                " document.documentElement.scrollHeight - window.innerHeight))",
                [selector, offset],
            )
        )
        for i in range(1, steps + 1):
            page.evaluate(
                "y => window.scrollTo(0, y)", round(start + (target - start) * ease(i / steps))
            )
            self.snap(ms // steps)


def record_verdict(browser: Browser, html: Path, out: Path) -> None:
    context = browser.new_context(
        viewport=STAGE, device_scale_factor=1, color_scheme="dark", bypass_csp=True
    )
    page = context.new_page()
    page.goto(html.as_uri())
    page.evaluate(JUDGE_OVERLAY_JS, JUDGE_OVERLAY_CSS)
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_timeout(600)
    j = Judge(Film(page, animations="allow"))
    page.evaluate("ms => window.__ej.time(ms)", 0)
    j.snap(500)

    j.status = "verdict"
    for ms in range(0, 1900, 90):
        page.evaluate("t => window.__ej.time(t)", ms)
        j.snap(80)
    page.evaluate("t => window.__ej.time(t)", 4000)
    j.snap(1800)

    j.status = "checking 5 claims"
    j.scroll_to("#claims-h", offset=70)
    j.snap(400)
    claims = int(page.evaluate("document.querySelectorAll('.claim').length"))
    callouts = {
        0: ("claim 1 · contradicted", "No POST /api/orders was ever sent", "#claim-1 .said"),
        2: ("claim 3 · contradicted", "“#1042” appears on no recorded page", "#claim-3 .said"),
    }
    for i in range(claims):
        j.status = f"claim {i + 1}/{claims}"
        j.revealed = i + 1
        j.flash = i
        j.lit = [f"#claim-{i + 1} .cite"]
        if i == 2:
            j.scroll_to("#claim-2", offset=70, ms=500, steps=8)
        if i == 4:
            j.scroll_to("#claim-4", offset=70, ms=500, steps=8)
        j.snap(140)
        j.flash = -1
        j.snap(120)
        if i in callouts:
            kicker, text, sel = callouts[i]
            for alpha in (0.4, 1.0):
                j.tags = [
                    {
                        "sel": sel,
                        "kicker": kicker,
                        "text": text,
                        "mode": "in",
                        "alpha": alpha,
                    }
                ]
                j.snap(60)
            j.snap(1900)
            j.tags = []
        else:
            j.snap(650)
    j.lit = []
    j.snap(300)

    j.status = "evidence timeline"
    j.scroll_to("#tl-h", offset=150, ms=1000)
    j.lit = ["#ev-net-23-3 .badge", "#ev-net-23-5 .badge"]
    j.snap(900)
    j.ghost = True
    j.snap(300)
    for alpha in (0.4, 1.0):
        j.tags = [
            {
                "sel": ".ej-ghost .badge",
                "kicker": "missing",
                "text": "The order request never happened",
                "dx": -24,
                "dy": 0,
                "alpha": alpha,
            }
        ]
        j.snap(60)
    j.status = "FAIL · 3 of 5 claims contradicted"
    j.snap(3600)
    context.close()
    save_animation(j.film.frames, out)


def render_social(browser: Browser, out: Path) -> None:
    context = browser.new_context(viewport=CARD, device_scale_factor=1)
    page = context.new_page()
    page.set_content(SOCIAL_HTML, wait_until="networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    raw = page.screenshot()
    context.close()
    optimise_png(raw, out)


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
