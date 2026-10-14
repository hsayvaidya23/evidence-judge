from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
RUNS = Path("tests") / "fixtures" / "runs"
BENCH_RESULTS = ROOT / "bench" / "results" / "offline-fixtures.json"

MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
DASH = chr(0x2013)
TIMES = chr(0xD7)
ELLIPSIS = "…"
PROMPT = chr(0x276F)
REDUCED_MOTION = "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"

INK = "#e8eaf2"
INK2 = "#a3abc0"
MUTED = "#69718a"
FAINT = "#3a4052"
VIOLET = "#8b5cf6"
VIOLET_SOFT = "#c4b5fd"
CYAN = "#22d3ee"
EMERALD = "#10b981"
ROSE = "#f43f5e"
AMBER = "#f59e0b"
BG_TOP = "#0d1017"
BG_BOTTOM = "#07080c"


def esc(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in {"-0", ""} else "0"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"README asset source changed: {message}")


def sans_w(text: str, size: float) -> float:
    return len(text) * size * 0.56


def mono_w(text: str, size: float) -> float:
    return len(text) * size * 0.602


class Timeline:
    def __init__(self, cycle: float) -> None:
        self.cycle = cycle
        self.rules: list[str] = []
        self.count = 0

    def pct(self, t: float) -> str:
        return f"{max(0.0, min(100.0, 100.0 * t / self.cycle)):.2f}%"

    def keyframes(self, frames: Sequence[tuple[float, str]], timing: str = "linear") -> str:
        name = f"k{self.count}"
        self.count += 1
        body = "".join(f"{self.pct(t)}{{{decl}}}" for t, decl in frames)
        self.rules.append(f"@keyframes {name}{{{body}}}")
        self.rules.append(f".{name}{{animation:{name} {num(self.cycle)}s {timing} infinite}}")
        return name

    def appear(self, start: float, fade: float = 0.25, end: float | None = None) -> str:
        stop = self.cycle - 0.6 if end is None else end
        return self.keyframes(
            [
                (0, "opacity:0"),
                (start, "opacity:0"),
                (start + fade, "opacity:1"),
                (stop, "opacity:1"),
                (min(stop + 0.35, self.cycle), "opacity:0"),
                (self.cycle, "opacity:0"),
            ]
        )

    def css(self) -> str:
        return "".join(self.rules)


def svg_doc(width: float, height: float, title: str, style: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(width)}" height="{num(height)}" '
        f'viewBox="0 0 {num(width)} {num(height)}" role="img" aria-label="{esc(title)}">'
        f"<title>{esc(title)}</title><style>{style}</style>{body}</svg>\n"
    )


def base_style() -> str:
    return (
        f"text{{font-family:{SANS};fill:{INK}}}"
        f".m{{font-family:{MONO}}}.i2{{fill:{INK2}}}.mu{{fill:{MUTED}}}"
        ".b{font-weight:700}.sb{font-weight:600}.xb{font-weight:800}.it{font-style:italic}"
        f".kick{{font-family:{MONO};font-size:11px;letter-spacing:.16em;font-weight:600}}"
    )


GLOW_FILTERS = (
    '<filter id="glow" x="-60%" y="-60%" width="220%" height="220%">'
    '<feGaussianBlur stdDeviation="5" result="b"/>'
    '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
    '<filter id="soft" x="-60%" y="-60%" width="220%" height="220%">'
    '<feGaussianBlur stdDeviation="2.2" result="b"/>'
    '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
    '<filter id="blur" x="-100%" y="-100%" width="300%" height="300%">'
    '<feGaussianBlur stdDeviation="14"/></filter>'
)


def card(
    width: float, height: float, *, dots: bool = True, aura: bool = True, surface: str = "url(#bg)"
) -> str:
    defs = (
        "<defs>"
        f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{BG_TOP}"/><stop offset="1" stop-color="{BG_BOTTOM}"/>'
        "</linearGradient>"
        '<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        '<circle cx="1" cy="1" r=".9" fill="#fff" fill-opacity=".055"/></pattern>'
        f'<radialGradient id="aura1" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" '
        f'gradientTransform="translate({num(width * 0.12)} 0) scale({num(width * 0.45)} {num(height * 0.9)})">'
        f'<stop offset="0" stop-color="{VIOLET}" stop-opacity=".16"/>'
        f'<stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="aura2" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" '
        f'gradientTransform="translate({num(width * 0.92)} {num(height)}) scale({num(width * 0.4)} {num(height * 0.8)})">'
        f'<stop offset="0" stop-color="{CYAN}" stop-opacity=".09"/>'
        f'<stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="rim" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{VIOLET}" stop-opacity="0"/>'
        f'<stop offset=".3" stop-color="{VIOLET}" stop-opacity=".75"/>'
        f'<stop offset=".7" stop-color="{CYAN}" stop-opacity=".75"/>'
        f'<stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>'
        f"{GLOW_FILTERS}</defs>"
    )
    full = f'<rect width="{num(width)}" height="{num(height)}" rx="18" fill="'
    layers = [f'{full}{surface}"/>']
    if aura:
        layers += [f'{full}url(#aura1)"/>', f'{full}url(#aura2)"/>']
    if dots:
        layers.append(f'{full}url(#dots)"/>')
    layers.append(
        f'<rect x=".5" y=".5" width="{num(width - 1)}" height="{num(height - 1)}" rx="17.5" '
        'fill="none" stroke="#fff" stroke-opacity=".09"/>'
        f'<path d="M{num(width * 0.18)} .75H{num(width * 0.82)}" stroke="url(#rim)" stroke-width="1.2"/>'
    )
    return defs + "".join(layers)


def glass(
    x: float, y: float, w: float, h: float, rx: float = 12, extra: str = "", alpha: float = 0.035
) -> str:
    return (
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="{num(rx)}" '
        f'fill="#fff" fill-opacity="{num(alpha)}" stroke="#fff" stroke-opacity=".1"{extra}/>'
    )


def pill(
    x: float,
    y: float,
    label: str,
    color: str,
    *,
    size: float = 11,
    h: float = 22,
    fill_opacity: float = 0.1,
    cls: str = "",
) -> tuple[str, float]:
    w = mono_w(label, size) + 18
    attr = f' class="{cls}"' if cls else ""
    return (
        f"<g{attr}>"
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="{num(h / 2)}" '
        f'fill="{color}" fill-opacity="{num(fill_opacity)}" stroke="{color}" stroke-opacity=".45"/>'
        f'<text class="m" x="{num(x + w / 2)}" y="{num(y + h / 2 + size * 0.36)}" '
        f'text-anchor="middle" style="font-size:{num(size)}px;fill:{color}">{esc(label)}</text></g>',
        w,
    )


def bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return x, y


def curve_d(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> str:
    return (
        f"M{num(p0[0])} {num(p0[1])}C{num(p1[0])} {num(p1[1])} {num(p2[0])} {num(p2[1])} "
        f"{num(p3[0])} {num(p3[1])}"
    )


def curve_len(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> float:
    pts = [bezier(p0, p1, p2, p3, k / 40) for k in range(41)]
    return sum(math.dist(a, b) for a, b in itertools.pairwise(pts))


@dataclass(frozen=True)
class JudgeRun:
    scenario: str
    command: list[str]
    stdout: list[str]
    verdict: dict[str, Any]
    exit_code: int

    @property
    def score(self) -> str:
        for line in self.stdout:
            found = re.search(r"score \d+/\d+", line)
            if found:
                return found.group(0)
        raise SystemExit(f"no score line in the {self.scenario} output")

    @property
    def outcome(self) -> str:
        return str(self.verdict["outcome"]).upper()

    def claims(self) -> list[dict[str, Any]]:
        return list(self.verdict["claims"])

    def reasons(self) -> str:
        parts = [str(c["reason"]) for c in self.claims()]
        parts += [str(e["reason"]) for e in self.verdict["expectations"]]
        return "\n".join(parts)

    def line(self, prefix: str) -> str:
        found = next((ln for ln in self.stdout if ln.startswith(prefix)), None)
        require(found is not None, f"no '{prefix}' line in the {self.scenario} output")
        return str(found)


JUDGE_ARGS = (
    "--trace",
    "trace.zip",
    "--har",
    "run.har",
    "--answer-file",
    "answer.txt",
    "--spec",
    "spec.yaml",
    "--offline",
)


def run_judge(scenario: str) -> JudgeRun:
    cwd = ROOT / RUNS / scenario
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "ANTHROPIC_API_KEY"}}
    env.update({"NO_COLOR": "1", "COLUMNS": "1000", "TERM": "dumb"})
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "verdict.json"
        proc = subprocess.run(
            [sys.executable, "-m", "evidence_judge", "judge", *JUDGE_ARGS, "--out", str(out)],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode not in (0, 1, 2):
            raise SystemExit(
                f"evidence-judge exited {proc.returncode} on {scenario}:\n{proc.stdout}"
            )
        verdict: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    lines = [ln.rstrip() for ln in proc.stdout.splitlines() if str(out) not in ln]
    return JudgeRun(
        scenario, ["evidence-judge", "judge", *JUDGE_ARGS], lines, verdict, proc.returncode
    )


@dataclass(frozen=True)
class Seg:
    col: int
    text: str
    cls: str = ""


@dataclass
class TermLine:
    segs: list[Seg]
    appear: float
    typing: tuple[int, int, float, float] | None = None
    band: str | None = None


TERM_COLS = 108
T_FS = 13.0
T_CW = 7.8
T_LH = 20.0
T_PAD = 26.0
T_HEAD = 42.0
T_SURFACE = "#0a0c12"
T_HEADER = "#10131c"

STATUS_LOOK = {
    "contradicted": ("✗", "rose"),
    "supported": ("✓", "emer"),
}


def word_cut(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    cut = text[: width - 2].rsplit(" ", 1)[0].rstrip(" ,;:(")
    return f"{cut} {ELLIPSIS}"


@dataclass
class TermScript:
    lines: list[TermLine] = field(default_factory=list)
    t: float = 0.5

    def prompt(self, cmd: str, *, cont: bool = False, rate: float = 0.016) -> None:
        segs = [Seg(4, cmd)] if cont else [Seg(0, PROMPT, "vio b"), Seg(2, cmd)]
        col = 4 if cont else 2
        start = self.t + (0.05 if cont else 0.35)
        stop = start + rate * len(cmd)
        self.lines.append(TermLine(segs, self.t, (col, len(cmd), start, stop)))
        self.t = stop + 0.2

    def out(self, segs: list[Seg], gap: float = 0.07, band: str | None = None) -> None:
        self.lines.append(TermLine(segs, self.t, band=band))
        self.t += gap


def claim_rows(run: JudgeRun) -> list[tuple[list[Seg], list[Seg], str | None]]:
    rows: list[tuple[list[Seg], list[Seg], str | None]] = []
    claim_col = 17
    for c in run.claims():
        status = str(c["status"])
        glyph, cls = STATUS_LOOK.get(status, ("?", "amb"))
        cites = " ".join(str(x["evidence_id"]) for x in c["citations"]) or "-"
        key = "[key] " if c["key"] else ""
        room = TERM_COLS - claim_col - len(cites) - 3
        first = [
            Seg(0, f"{glyph} {status}", f"{cls} b"),
            Seg(claim_col, key, "vio"),
            Seg(claim_col + len(key), word_cut(str(c["claim_text"]), room - len(key))),
            Seg(TERM_COLS - len(cites), cites, "cyan"),
        ]
        reason = str(c["reason"]).split("; ")[0]
        second = [
            Seg(claim_col, "↳", "mu"),
            Seg(claim_col + 2, word_cut(reason, TERM_COLS - claim_col - 2), "i2"),
        ]
        band = "rose" if status == "contradicted" else "emer" if status == "supported" else None
        rows.append((first, second, band))
    if any(b == "rose" for _, _, b in rows):
        rows = [(a, b, band if band == "rose" else None) for a, b, band in rows]
    return rows


def dimension_segs(line: str) -> list[Seg]:
    segs = [Seg(0, "Dimensions", "mu")]
    for found in re.finditer(r"(\w+) (\d+)%", line):
        value = int(found.group(2))
        cls = "emer" if value >= 100 else "rose" if value < 50 else "amb"
        segs.append(Seg(found.start(1), found.group(1), "i2"))
        segs.append(Seg(found.start(2), f"{value}%", f"{cls} b"))
    return segs


def folded_summary(run: JudgeRun) -> Seg | None:
    sections: list[tuple[str, int]] = []
    in_tail = False
    for ln in run.stdout:
        if ln.startswith("└"):
            in_tail = True
            continue
        if not in_tail or ln.startswith("llm calls"):
            continue
        if ln.startswith(" "):
            if sections:
                sections[-1] = (sections[-1][0], sections[-1][1] + 1)
        else:
            name = "dimension findings" if ln.startswith("Dimensions") else ln.lower()
            sections.append((name, 0))
    parts = [f"{name} ({n})" for name, n in sections if n]
    if not parts:
        return None
    total = sum(n for _, n in sections)
    return Seg(0, f"⋯ {total} more lines folded: {', '.join(parts)}", "mu it")


def render_terminal(run: JudgeRun, title: str, *, hold: float) -> str:
    script = TermScript()
    script.prompt(f"cd {(RUNS / run.scenario).as_posix()}")
    cmd = " ".join(run.command)
    head, tail = cmd.split(" --answer-file ")
    script.prompt(f"{head} \\")
    script.prompt(f"--answer-file {tail}", cont=True)
    script.t += 0.5

    redacted = next((ln for ln in run.stdout if ln.startswith("redacted ")), None)
    if redacted:
        script.out([Seg(0, redacted, "mu")])
    badge_line = run.stdout[1 if redacted else 0].split()
    require(badge_line[0] == run.outcome, "badge line does not start with the outcome")
    out_cls = "rose" if run.outcome == "FAIL" else "emer" if run.outcome == "PASS" else "amb"
    rest = " ".join(badge_line[1:])
    score_txt = run.score
    script.out(
        [
            Seg(0, f" {run.outcome} ", f"badge-{out_cls}"),
            Seg(len(run.outcome) + 3, score_txt, "b"),
            Seg(len(run.outcome) + 5 + len(score_txt), rest.replace(score_txt, "").strip(), "mu"),
        ],
        gap=0.12,
    )
    task = run.line("task ")
    script.out([Seg(0, "task", "mu"), Seg(5, task[5:])])
    summary = str(run.verdict["summary"])
    require(summary in run.stdout, "the printed headline differs from the verdict summary")
    label, detail = summary.split(": ", 1)
    clause, *more = detail.split("; ")
    width = TERM_COLS - len(label) - 2
    shown = textwrap.wrap(clause + (f"; {ELLIPSIS}" if more else ""), width)
    if len(shown) > 2:
        shown = [shown[0], word_cut(" ".join(shown[1:]), width)]
    for k, text in enumerate(shown):
        segs = [Seg(len(label) + 2, text, "")]
        if k == 0:
            segs.insert(0, Seg(0, f"{label}:", f"{out_cls} b"))
        script.out(segs, gap=0.1)
    script.out([], gap=0.05)
    script.out(
        [
            Seg(0, "STATUS", "mu hd"),
            Seg(17, "CLAIM · REASON", "mu hd"),
            Seg(TERM_COLS - 8, "EVIDENCE", "mu hd"),
        ],
        gap=0.05,
    )
    script.out([Seg(0, "─" * TERM_COLS, "rule")], gap=0.05)
    table_start = len(script.lines)
    for first, second, band in claim_rows(run):
        script.out(first, gap=0.09, band=band)
        script.out(second, gap=0.09, band=band)
    table_end = len(script.lines)
    script.out([Seg(0, "─" * TERM_COLS, "rule")], gap=0.12)
    band_at = script.t
    script.out(dimension_segs(run.line("Dimensions")), gap=0.1)
    fold = folded_summary(run)
    if fold:
        script.out([fold], gap=0.1)
    usage = run.line("llm calls")
    script.out([Seg(0, usage, "mu")], gap=0.1)
    script.t += 0.8
    script.prompt("echo $?", rate=0.07)
    code_cls = "rose" if run.exit_code else "emer"
    script.out([Seg(0, str(run.exit_code), f"{code_cls} b")], gap=0.3)
    final = TermLine([Seg(0, PROMPT, "vio b")], script.t)
    script.lines.append(final)
    cycle = script.t + hold

    tl = Timeline(cycle)
    card_w = T_PAD * 2 + TERM_COLS * T_CW
    body_top = T_HEAD + 16
    card_h = body_top + len(script.lines) * T_LH + 18
    body: list[str] = []

    blocks: list[tuple[str, int, int]] = []
    for i in range(table_start, table_end):
        band = script.lines[i].band
        if band is None:
            continue
        if blocks and blocks[-1][0] == band and blocks[-1][2] == i:
            blocks[-1] = (band, blocks[-1][1], i + 1)
        else:
            blocks.append((band, i, i + 1))
    for band, first, last in blocks:
        y_top = body_top + first * T_LH - 1
        h = (last - first) * T_LH + 2
        color = ROSE if band == "rose" else EMERALD
        base, peak = (0.1, 0.26) if band == "rose" else (0.07, 0.16)
        frames: list[tuple[float, str]] = [
            (0, "opacity:0"),
            (band_at, "opacity:0"),
            (band_at + 0.25, f"opacity:{peak}"),
            (band_at + 1.0, f"opacity:{base}"),
            (cycle - 0.6, f"opacity:{base}"),
            (cycle - 0.25, "opacity:0"),
            (cycle, "opacity:0"),
        ]
        x0 = T_PAD - 10
        body.append(
            f'<rect class="{tl.keyframes(frames)}" x="{num(x0)}" y="{num(y_top)}" '
            f'width="{num(TERM_COLS * T_CW + 20)}" height="{num(h)}" rx="6" fill="{color}" '
            f'style="opacity:{base}"/>'
        )
        edge = tl.appear(band_at, fade=0.25, end=cycle - 0.6)
        body.append(
            f'<rect class="{edge}" x="{num(x0)}" y="{num(y_top + 3)}" width="2.5" '
            f'height="{num(h - 6)}" rx="1.25" fill="{color}" filter="url(#soft)"/>'
        )

    for i, line in enumerate(script.lines):
        y_top = body_top + i * T_LH
        cls = tl.appear(line.appear, fade=0.06, end=cycle - 0.6)
        body.append(f'<g class="{cls}">{render_segs(line.segs, y_top)}')
        if line.typing is not None:
            col, n, start, stop = line.typing
            x = T_PAD + col * T_CW
            shift = f"translateX({num(n * T_CW)}px)"
            cover = tl.keyframes(
                [
                    (0, "opacity:1;transform:none"),
                    (start, f"opacity:1;transform:none;animation-timing-function:steps({n},end)"),
                    (stop, f"opacity:1;transform:{shift}"),
                    (stop + 0.01, f"opacity:0;transform:{shift}"),
                    (cycle, f"opacity:0;transform:{shift}"),
                ]
            )
            body.append(
                f'<rect class="{cover}" x="{num(x)}" y="{num(y_top)}" '
                f'width="{num((n + 1) * T_CW)}" height="{num(T_LH)}" fill="{T_SURFACE}" '
                'style="opacity:0"/>'
            )
            nxt = script.lines[i + 1].appear if i + 1 < len(script.lines) else cycle
            cursor = tl.keyframes(
                [
                    (0, "opacity:0;transform:none"),
                    (line.appear, "opacity:0;transform:none"),
                    (line.appear + 0.01, "opacity:.9;transform:none"),
                    (start, f"opacity:.9;transform:none;animation-timing-function:steps({n},end)"),
                    (stop, f"opacity:.9;transform:{shift}"),
                    (nxt, f"opacity:.9;transform:{shift}"),
                    (nxt + 0.01, f"opacity:0;transform:{shift}"),
                    (cycle, "opacity:0"),
                ]
            )
            body.append(
                f'<rect class="{cursor}" x="{num(x)}" y="{num(y_top + 3)}" width="{num(T_CW)}" '
                f'height="{num(T_LH - 6)}" rx="1" fill="{CYAN}" style="opacity:0"/>'
            )
        if line is final:
            body.append(
                f'<rect class="blink" x="{num(T_PAD + 2 * T_CW)}" y="{num(y_top + 3)}" '
                f'width="{num(T_CW)}" height="{num(T_LH - 6)}" rx="1" fill="{CYAN}" opacity=".9"/>'
            )
        body.append("</g>")

    lights = "".join(
        f'<circle cx="{num(22 + k * 18)}" cy="{num(T_HEAD / 2)}" r="5.5" fill="{c}" '
        'fill-opacity=".9"/>'
        for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840"))
    )
    tab = f"{run.scenario} — evidence-judge"
    tab_w = mono_w(tab, 11.5) + 34
    header = (
        f'<path d="M0 18a18 18 0 0 1 18-18h{num(card_w - 36)}a18 18 0 0 1 18 18v{num(T_HEAD - 18)}'
        f'h-{num(card_w)}z" fill="{T_HEADER}"/>'
        f'<line x1="0" x2="{num(card_w)}" y1="{num(T_HEAD)}" y2="{num(T_HEAD)}" stroke="#fff" '
        'stroke-opacity=".07"/>'
        f'<path d="M{num(card_w * 0.25)} {num(T_HEAD)}H{num(card_w * 0.75)}" stroke="url(#rim)" '
        'stroke-opacity=".7"/>'
        f"{lights}"
        f'<rect x="{num(card_w / 2 - tab_w / 2)}" y="9" width="{num(tab_w)}" height="24" rx="7" '
        'fill="#fff" fill-opacity=".045" stroke="#fff" stroke-opacity=".08"/>'
        f'<circle cx="{num(card_w / 2 - tab_w / 2 + 13)}" cy="21" r="3" fill="{CYAN}" '
        'filter="url(#soft)"/>'
        f'<text class="m" x="{num(card_w / 2 + 6)}" y="25" text-anchor="middle" '
        f'style="font-size:11.5px;fill:{INK2}">{esc(tab)}</text>'
        f'<text class="m" x="{num(card_w - 20)}" y="25" text-anchor="end" '
        f'style="font-size:11px;fill:{MUTED}">zsh</text>'
    )
    style = (
        f"text{{font-family:{MONO};font-size:{num(T_FS)}px;fill:{INK};white-space:pre}}"
        f".b{{font-weight:700}}.it{{font-style:italic}}.mu{{fill:{MUTED}}}.i2{{fill:{INK2}}}"
        f".vio{{fill:{VIOLET_SOFT}}}.cyan{{fill:{CYAN}}}.rose{{fill:{ROSE}}}.emer{{fill:{EMERALD}}}"
        f".amb{{fill:{AMBER}}}.hd{{font-size:11px;letter-spacing:.12em}}.rule{{fill:{FAINT}}}"
        "@keyframes blink{0%,49%{opacity:.9}50%,100%{opacity:0}}"
        ".blink{animation:blink 1.05s step-end infinite}" + tl.css() + REDUCED_MOTION
    )
    frame = card(card_w, card_h, dots=False, aura=False, surface=T_SURFACE)
    return svg_doc(card_w, card_h, title, style, frame + header + "".join(body))


BADGE_FILL = {"rose": ROSE, "emer": EMERALD, "amb": AMBER}


def render_segs(segs: Sequence[Seg], y_top: float) -> str:
    parts: list[str] = []
    baseline = y_top + T_LH * 0.7
    for seg in segs:
        if seg.cls.startswith("badge-"):
            color = BADGE_FILL[seg.cls[6:]]
            x = T_PAD + seg.col * T_CW
            w = len(seg.text) * T_CW
            parts.append(
                f'<rect x="{num(x - 2)}" y="{num(y_top + 1)}" width="{num(w + 4)}" '
                f'height="{num(T_LH - 2)}" rx="5" fill="{color}" filter="url(#soft)"/>'
                f'<text x="{num(x + w / 2)}" y="{num(baseline)}" text-anchor="middle" class="b" '
                f'style="fill:#fff;letter-spacing:.08em">{esc(seg.text.strip())}</text>'
            )
            continue
        stripped = seg.text.rstrip(" ")
        if not stripped:
            continue
        attrs = f' class="{seg.cls}"' if seg.cls else ""
        if seg.cls == "rule":
            parts.append(
                f'<line x1="{num(T_PAD)}" x2="{num(T_PAD + len(stripped) * T_CW)}" '
                f'y1="{num(y_top + T_LH / 2)}" y2="{num(y_top + T_LH / 2)}" stroke="#fff" '
                'stroke-opacity=".14"/>'
            )
            continue
        length = f' textLength="{num(len(stripped) * T_CW)}" lengthAdjust="spacingAndGlyphs"'
        parts.append(
            f'<text x="{num(T_PAD + seg.col * T_CW)}" y="{num(baseline)}"{attrs}{length} '
            f'xml:space="preserve">{esc(stripped)}</text>'
        )
    return "".join(parts)


def lcg(seed: int) -> Any:
    state = seed

    def rnd() -> float:
        nonlocal state
        state = (state * 1103515245 + 12345) % 2147483648
        return state / 2147483648

    return rnd


def particles(
    tl_rules: list[str], box: tuple[float, float, float, float], count: int, seed: int
) -> str:
    rnd = lcg(seed)
    x0, y0, w, h = box
    out: list[str] = []
    for k in range(count):
        x = x0 + rnd() * w
        y = y0 + rnd() * h
        r = 0.8 + rnd() * 1.3
        dur = 5 + rnd() * 5
        delay = -rnd() * dur
        drift = 8 + rnd() * 14
        color = CYAN if k % 3 else VIOLET_SOFT
        name = f"p{seed}_{k}"
        tl_rules.append(
            f"@keyframes {name}{{0%{{transform:translateY(0);opacity:0}}"
            f"30%{{opacity:.7}}70%{{opacity:.5}}"
            f"100%{{transform:translateY(-{num(drift)}px);opacity:0}}}}"
            f".{name}{{animation:{name} {num(dur)}s ease-in-out {num(delay)}s infinite}}"
        )
        out.append(
            f'<circle class="{name}" cx="{num(x)}" cy="{num(y)}" r="{num(r)}" fill="{color}" '
            'style="opacity:.35"/>'
        )
    return "".join(out)


@dataclass(frozen=True)
class Evidence:
    eid: str
    label: str


def hero_facts(forged: JudgeRun) -> tuple[str, list[Evidence], str]:
    claims = forged.claims()
    order = claims[0]
    quote = str(order["quote"])
    require("placed the order" in quote, "first forged claim no longer quotes the order")
    cites = {str(c["evidence_id"]): str(c["excerpt"]) for c in order["citations"]}
    require(cites.get("net#3") == "POST /api/cart → 200", "net#3 is no longer POST /api/cart")
    reasons = forged.reasons()
    final_url = re.search(r"final URL is (\S+)", reasons)
    require(final_url is not None, "no final URL in the forged reasons")
    url = final_url.group(1) if final_url else ""
    supported = next(c for c in claims if c["status"] == "supported")
    snap_ids = [str(c["evidence_id"]) for c in supported["citations"]]
    require("snap#6" in snap_ids and f"latest at {url}" in str(supported["reason"]), "snap#6 moved")
    require("URL contains '/api/orders'" in reasons, "the /api/orders expectation changed")
    evidence = [
        Evidence("net#3", "POST /api/cart → 200"),
        Evidence("snap#6", url),
        Evidence("final", url),
    ]
    return "I placed the order ✅", evidence, "no POST /api/orders"


def render_hero(forged: JudgeRun, bench: dict[str, Any]) -> str:
    width, height = 1200.0, 380.0
    cycle = 11.0
    tl = Timeline(cycle)
    extra_rules: list[str] = []
    claim_label, evidence, stamp_reason = hero_facts(forged)
    out: list[str] = [card(width, height)]

    out.append(
        '<defs><linearGradient id="wm" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/>'
        "</linearGradient>"
        '<linearGradient id="scan" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{CYAN}" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="{CYAN}" stop-opacity=".07"/>'
        f'<stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>'
        '<clipPath id="panel"><rect x="580" y="28" width="592" height="324" rx="16"/></clipPath>'
        "</defs>"
    )

    lx = 60.0
    out.append(
        f'<circle cx="{num(lx + 4)}" cy="98" r="3.5" fill="{CYAN}" filter="url(#soft)"/>'
        f'<circle class="ping" cx="{num(lx + 4)}" cy="98" r="3.5" fill="none" stroke="{CYAN}"/>'
        f'<text class="kick" x="{num(lx + 18)}" y="102" style="fill:{CYAN}">'
        "CLAIMS → EVIDENCE → VERDICT</text>"
        f'<text x="{num(lx - 3)}" y="170" class="xb" style="font-size:60px;letter-spacing:-.025em">'
        'Evidence <tspan fill="url(#wm)">Judge</tspan></text>'
        f'<text x="{num(lx)}" y="218" class="i2" style="font-size:22px">'
        "Don&#8217;t trust what the agent says.</text>"
        f'<text x="{num(lx)}" y="248" class="sb" style="font-size:22px">Check what it did.</text>'
    )

    pooled = [m for m in bench["forgery_metrics"] if m["kind"] == "all manipulations"]
    require(bool(pooled), "bench results have no pooled row")
    passed = max(int(m["false_pass"]["k"]) for m in pooled)
    n_forged = int(pooled[0]["false_pass"]["n"])
    usage = forged.verdict["usage"]
    det = round(100 * float(forged.verdict["deterministic_share"]))
    stats = [
        (EMERALD, f"{passed}/{n_forged} forged runs passed"),
        (CYAN, f"{det}% deterministic"),
        (VIOLET_SOFT, f"{usage['llm_calls']} LLM calls offline"),
    ]
    sx = lx
    for color, label in stats:
        w = mono_w(label, 11.5) + 34
        out.append(
            glass(sx, 286, w, 30, rx=15)
            + f'<circle cx="{num(sx + 15)}" cy="301" r="3.5" fill="{color}" filter="url(#soft)"/>'
            f'<text class="m" x="{num(sx + 25)}" y="305" style="font-size:11.5px;fill:{INK2}">'
            f"{esc(label)}</text>"
        )
        sx += w + 10

    px, py, pw, ph = 580.0, 28.0, 592.0, 324.0
    out.append(glass(px, py, pw, ph, rx=16, alpha=0.03))
    out.append(
        f'<rect class="scan" x="{num(px)}" y="{num(py - 60)}" width="{num(pw)}" height="60" '
        'fill="url(#scan)" clip-path="url(#panel)" style="opacity:0"/>'
    )
    out.append(
        f'<line x1="{num(px)}" x2="{num(px + pw)}" y1="{num(py + 34)}" y2="{num(py + 34)}" '
        'stroke="#fff" stroke-opacity=".07"/>'
        f'<circle class="live" cx="{num(px + 18)}" cy="{num(py + 17)}" r="3.5" fill="{ROSE}"/>'
        f'<text class="m" x="{num(px + 30)}" y="{num(py + 21)}" style="font-size:11px;fill:{INK2}">'
        f'LIVE <tspan fill="{MUTED}">· run {esc(forged.scenario)} · trace.zip + run.har'
        "</tspan></text>"
        f'<text class="m" x="{num(px + pw - 16)}" y="{num(py + 21)}" text-anchor="end" '
        f'style="font-size:11px;fill:{MUTED}">evidence-judge judge --offline</text>'
    )
    out.append(particles(extra_rules, (px + 10, py + 50, pw - 20, ph - 70), 18, 7))

    agent = (650.0, 200.0)
    judge = (1078.0, 200.0)
    ev_x = 762.0
    ev_ys = [236.0, 270.0, 304.0]
    ev_nodes = [
        (e, ev_x, y, mono_w(f"{e.eid}  {e.label}", 11) + 22)
        for e, y in zip(evidence, ev_ys, strict=True)
    ]

    p0 = (agent[0] + 24, agent[1] - 18)
    p3 = (judge[0] - 30, judge[1] - 18)
    c1 = (p0[0] + 40, 64.0)
    c2 = (p3[0] - 40, 64.0)
    main_d = curve_d(p0, c1, c2, p3)
    out.append(
        f'<path d="{main_d}" fill="none" stroke="{VIOLET}" stroke-opacity=".25" stroke-width="1.5"/>'
        f'<path class="flow" d="{main_d}" fill="none" stroke="{VIOLET_SOFT}" stroke-width="1.6" '
        'stroke-dasharray="3 9" stroke-linecap="round" stroke-opacity=".8"/>'
    )
    for _, x, y, _ in ev_nodes:
        a_d = curve_d((agent[0], agent[1] + 30), (agent[0], y), (agent[0] + 40, y), (x, y))
        out.append(
            f'<path d="{a_d}" fill="none" stroke="#fff" stroke-opacity=".14" stroke-width="1" '
            'stroke-dasharray="2 5"/>'
        )
    out.append(
        f'<text class="kick" x="{num(ev_x + 2)}" y="{num(ev_ys[0] - 24)}" '
        f'style="fill:{MUTED};font-size:9.5px">RECORDED EVIDENCE</text>'
    )

    t_emit, t_dock = 0.5, 2.5
    dock_t = 0.5
    t_ev = [2.9, 3.4, 3.9]
    t_stamp = 4.9
    t_fail = 5.4
    out_at = cycle - 0.6

    for (e, x, y, w), t0 in zip(ev_nodes, t_ev, strict=True):
        start = (x + w, y)
        q1 = (x + w + 50, y)
        q2 = (judge[0] - 80, judge[1])
        q3 = (judge[0] - 35, judge[1])
        d = curve_d(start, q1, q2, q3)
        length = curve_len(start, q1, q2, q3)
        out.append(
            f'<path d="{d}" fill="none" stroke="{CYAN}" stroke-opacity=".14" stroke-width="1.2"/>'
        )
        draw = tl.keyframes(
            [
                (0, f"stroke-dashoffset:{num(length)};opacity:1"),
                (t0, f"stroke-dashoffset:{num(length)};opacity:1"),
                (t0 + 0.55, "stroke-dashoffset:0;opacity:1"),
                (out_at, "stroke-dashoffset:0;opacity:1"),
                (out_at + 0.4, "stroke-dashoffset:0;opacity:0"),
                (cycle, f"stroke-dashoffset:{num(length)};opacity:0"),
            ],
            timing="ease-out",
        )
        out.append(
            f'<path class="{draw}" d="{d}" fill="none" stroke="{CYAN}" stroke-width="1.6" '
            f'stroke-dasharray="{num(length)}" filter="url(#soft)" style="stroke-dashoffset:0"/>'
        )
        steps = 8
        hidden = f"opacity:0;transform:translate({num(start[0])}px,{num(start[1])}px)"
        frames: list[tuple[float, str]] = [(0, hidden), (t0 - 0.01, hidden)]
        for k in range(steps + 1):
            bx, by = bezier(start, q1, q2, q3, k / steps)
            frames.append(
                (
                    t0 + 0.55 * k / steps,
                    f"opacity:{1 if k < steps else 0};transform:translate({num(bx)}px,{num(by)}px)",
                )
            )
        frames.append((cycle, "opacity:0"))
        out.append(
            f'<circle class="{tl.keyframes(frames)}" r="3.2" fill="#fff" filter="url(#soft)" '
            'style="opacity:0"/>'
        )
        lit = tl.appear(t0 - 0.1, fade=0.3, end=out_at)
        out.append(
            f'<rect x="{num(x)}" y="{num(y - 13)}" width="{num(w)}" height="26" rx="8" '
            'fill="#0f1320" stroke="#fff" stroke-opacity=".12"/>'
            f'<g class="{lit}"><rect x="{num(x)}" y="{num(y - 13)}" width="{num(w)}" height="26" '
            f'rx="8" fill="{CYAN}" fill-opacity=".1" stroke="{CYAN}" stroke-opacity=".7" '
            'filter="url(#soft)"/></g>'
            f'<text class="m" x="{num(x + 11)}" y="{num(y + 4)}" style="font-size:11px;fill:{INK2}">'
            f'<tspan fill="{CYAN}" font-weight="700">{esc(e.eid)}</tspan>  {esc(e.label)}</text>'
        )

    out.append(
        f'<circle class="pulse" cx="{num(agent[0])}" cy="{num(agent[1])}" r="30" fill="none" '
        f'stroke="{VIOLET}" stroke-width="1.5"/>'
        f'<circle cx="{num(agent[0])}" cy="{num(agent[1])}" r="40" fill="{VIOLET}" '
        'fill-opacity=".18" filter="url(#blur)"/>'
        f'<circle cx="{num(agent[0])}" cy="{num(agent[1])}" r="30" fill="#140f2a" stroke="{VIOLET}" '
        'stroke-width="1.6"/>'
        f'<rect x="{num(agent[0] - 13)}" y="{num(agent[1] - 9)}" width="26" height="19" rx="6" '
        f'fill="none" stroke="{VIOLET_SOFT}" stroke-width="1.8"/>'
        f'<line x1="{num(agent[0])}" x2="{num(agent[0])}" y1="{num(agent[1] - 9)}" '
        f'y2="{num(agent[1] - 15)}" stroke="{VIOLET_SOFT}" stroke-width="1.8"/>'
        f'<circle cx="{num(agent[0])}" cy="{num(agent[1] - 16.5)}" r="2" fill="{VIOLET_SOFT}"/>'
        f'<circle class="eyes" cx="{num(agent[0] - 5)}" cy="{num(agent[1])}" r="2.2" fill="{VIOLET_SOFT}"/>'
        f'<circle class="eyes" cx="{num(agent[0] + 5)}" cy="{num(agent[1])}" r="2.2" fill="{VIOLET_SOFT}"/>'
        f'<text class="kick" x="{num(agent[0])}" y="{num(agent[1] + 52)}" text-anchor="middle" '
        f'style="fill:{VIOLET_SOFT}">AGENT</text>'
    )

    rose_on = tl.appear(t_stamp, fade=0.2, end=out_at)
    scan_on = tl.keyframes(
        [
            (0, "opacity:0"),
            (t_dock, "opacity:0"),
            (t_dock + 0.3, "opacity:1"),
            (t_stamp, "opacity:1"),
            (t_stamp + 0.3, "opacity:0"),
            (cycle, "opacity:0"),
        ]
    )
    jx, jy = judge
    out.append(
        f'<circle cx="{num(jx)}" cy="{num(jy)}" r="46" fill="{CYAN}" fill-opacity=".14" '
        'filter="url(#blur)"/>'
        f'<g class="{rose_on}"><circle cx="{num(jx)}" cy="{num(jy)}" r="50" fill="{ROSE}" '
        'fill-opacity=".22" filter="url(#blur)"/></g>'
        f'<circle class="spin" cx="{num(jx)}" cy="{num(jy)}" r="44" fill="none" stroke="{CYAN}" '
        'stroke-opacity=".45" stroke-dasharray="2 7" stroke-width="1.4"/>'
        f'<circle cx="{num(jx)}" cy="{num(jy)}" r="34" fill="#071a20" stroke="{CYAN}" '
        'stroke-width="1.8"/>'
        f'<g class="{rose_on}"><circle cx="{num(jx)}" cy="{num(jy)}" r="34" fill="#240a12" '
        f'stroke="{ROSE}" stroke-width="2"/></g>'
        f'<g class="{scan_on}" style="opacity:0"><circle cx="{num(jx)}" cy="{num(jy)}" r="34" '
        f'fill="none" stroke="#fff" stroke-width="2" filter="url(#soft)"/></g>'
    )
    for dx in (-10, 10):
        out.append(
            f'<circle cx="{num(jx + dx)}" cy="{num(jy - 2)}" r="8.5" fill="none" stroke="{CYAN}" '
            'stroke-width="2"/>'
            f'<circle class="look" cx="{num(jx + dx)}" cy="{num(jy - 2)}" r="3.6" fill="{CYAN}" '
            'filter="url(#soft)"/>'
        )
    out.append(
        f'<path d="M{num(jx)} {num(jy + 6)}l-3.5 5h7z" fill="{AMBER}"/>'
        f'<text class="kick" x="{num(jx)}" y="{num(jy - 48)}" text-anchor="middle" '
        f'style="fill:{CYAN}">JUDGE</text>'
    )

    chip_w = sans_w(claim_label, 13.5) + 40
    chip_h = 30.0
    frames = [(0.0, f"opacity:0;transform:translate({num(p0[0])}px,{num(p0[1])}px) scale(.6)")]
    frames.append(
        (t_emit, f"opacity:0;transform:translate({num(p0[0])}px,{num(p0[1])}px) scale(.6)")
    )
    steps = 12
    for k in range(steps + 1):
        frac = dock_t * k / steps
        bx, by = bezier(p0, c1, c2, p3, frac)
        ease = 0.5 - 0.5 * math.cos(math.pi * k / steps)
        tt = t_emit + 0.25 + (t_dock - t_emit - 0.25) * ease
        frames.append((tt, f"opacity:1;transform:translate({num(bx)}px,{num(by)}px) scale(1)"))
    dock = bezier(p0, c1, c2, p3, dock_t)
    dock_tf = f"translate({num(dock[0])}px,{num(dock[1])}px)"
    frames += [
        (t_stamp + 0.15, f"opacity:1;transform:{dock_tf} scale(1)"),
        (t_stamp + 0.2, f"opacity:1;transform:{dock_tf} translateX(-4px) scale(1)"),
        (t_stamp + 0.26, f"opacity:1;transform:{dock_tf} translateX(4px) scale(1)"),
        (t_stamp + 0.32, f"opacity:1;transform:{dock_tf} translateX(-2px) scale(1)"),
        (t_stamp + 0.38, f"opacity:1;transform:{dock_tf} scale(1)"),
        (out_at, f"opacity:1;transform:{dock_tf} scale(1)"),
        (out_at + 0.35, f"opacity:0;transform:{dock_tf} scale(1)"),
        (cycle, f"opacity:0;transform:{dock_tf} scale(1)"),
    ]
    chip_cls = tl.keyframes(frames)
    chip_rose = tl.appear(t_stamp + 0.15, fade=0.15, end=out_at)
    out.append(
        f'<g class="{chip_cls}" style="transform:{dock_tf}">'
        f'<rect x="{num(-chip_w / 2)}" y="{num(-chip_h / 2)}" width="{num(chip_w)}" '
        f'height="{num(chip_h)}" rx="{num(chip_h / 2)}" fill="#1a1433" stroke="{VIOLET}" '
        'stroke-width="1.4" filter="url(#soft)"/>'
        f'<g class="{chip_rose}"><rect x="{num(-chip_w / 2)}" y="{num(-chip_h / 2)}" '
        f'width="{num(chip_w)}" height="{num(chip_h)}" rx="{num(chip_h / 2)}" fill="#2a0d18" '
        f'stroke="{ROSE}" stroke-width="1.6"/></g>'
        f'<text x="0" y="5" text-anchor="middle" class="sb" style="font-size:13.5px">'
        f"“{esc(claim_label)}”</text>"
        f'<g class="{chip_rose}"><line x1="{num(-chip_w / 2 + 16)}" x2="{num(chip_w / 2 - 16)}" '
        f'y1="0" y2="0" stroke="{ROSE}" stroke-width="1.6"/></g></g>'
    )

    stamp = f"CONTRADICTED · {stamp_reason}"
    st_w = mono_w(stamp, 12) + 30
    slam = tl.keyframes(
        [
            (0, "opacity:0;transform:scale(1.9)"),
            (
                t_stamp,
                "opacity:0;transform:scale(1.9);animation-timing-function:cubic-bezier(.6,0,.9,.4)",
            ),
            (t_stamp + 0.18, "opacity:1;transform:scale(.95)"),
            (t_stamp + 0.3, "opacity:1;transform:scale(1.03)"),
            (t_stamp + 0.42, "opacity:1;transform:scale(1)"),
            (out_at, "opacity:1;transform:scale(1)"),
            (out_at + 0.35, "opacity:0;transform:scale(1)"),
            (cycle, "opacity:0"),
        ]
    )
    out.append(
        f'<g transform="translate({num(dock[0])} {num(dock[1] + 40)}) rotate(-3)">'
        f'<g class="{slam}" style="transform-box:view-box;transform-origin:0 0">'
        f'<rect x="{num(-st_w / 2)}" y="-14" width="{num(st_w)}" height="28" rx="6" fill="#1d0710" '
        f'stroke="{ROSE}" stroke-width="2" filter="url(#soft)"/>'
        f'<text class="m b" x="0" y="4.5" text-anchor="middle" style="font-size:12px;fill:{ROSE};'
        f'letter-spacing:.06em">{esc(stamp)}</text></g></g>'
    )

    verdict = f"{forged.outcome} · {forged.score.split()[1]}"
    v_w = mono_w(verdict, 11.5) + 22
    fail_on = tl.appear(t_fail, fade=0.3, end=out_at)
    out.append(
        f'<g class="{fail_on}">'
        f'<rect x="{num(jx - v_w / 2)}" y="{num(jy + 46)}" width="{num(v_w)}" height="24" rx="12" '
        f'fill="{ROSE}" filter="url(#soft)"/>'
        f'<text class="m b" x="{num(jx)}" y="{num(jy + 62)}" text-anchor="middle" '
        f'style="font-size:11.5px;fill:#fff">{esc(verdict)}</text></g>'
    )

    style = (
        base_style()
        + "@keyframes ping{0%{transform:scale(1);opacity:.8}100%{transform:scale(3.4);opacity:0}}"
        ".ping{transform-box:fill-box;transform-origin:center;animation:ping 2.2s ease-out infinite}"
        "@keyframes pulse{0%{transform:scale(1);opacity:.7}100%{transform:scale(1.7);opacity:0}}"
        ".pulse{transform-box:fill-box;transform-origin:center;animation:pulse 2.6s ease-out infinite}"
        "@keyframes spin{to{transform:rotate(360deg)}}"
        ".spin{transform-box:fill-box;transform-origin:center;animation:spin 18s linear infinite}"
        "@keyframes flow{to{stroke-dashoffset:-48}}"
        ".flow{animation:flow 1.6s linear infinite}"
        f"@keyframes scan{{0%{{transform:translateY(0);opacity:0}}10%{{opacity:1}}"
        f"90%{{opacity:1}}100%{{transform:translateY({num(ph + 60)}px);opacity:0}}}}"
        ".scan{animation:scan 5.5s ease-in-out infinite}"
        "@keyframes live{0%,100%{opacity:1}50%{opacity:.25}}"
        ".live{animation:live 1.6s ease-in-out infinite}"
        "@keyframes look{0%,40%,100%{transform:none}50%,85%{transform:translateX(-2px)}}"
        ".look{animation:look 6s ease-in-out infinite}"
        + "".join(extra_rules)
        + tl.css()
        + REDUCED_MOTION
    )
    title = "Evidence Judge: don't trust what the agent says, check what it did"
    return svg_doc(width, height, title, style, "".join(out))


@dataclass(frozen=True)
class Stage:
    n: int
    title: str
    desc: tuple[str, str]
    start: float


def pipeline_facts(forged: JudgeRun) -> dict[str, Any]:
    reasons = forged.reasons()
    requests = re.search(r"among (\d+) captured requests", reasons)
    states = re.search(r"None of (\d+) recorded page states", reasons)
    redacted = re.match(r"redacted (\d+) values \((\w+) (\d+)\)", forged.stdout[0])
    require(requests is not None and states is not None, "request/page-state counts moved")
    require(redacted is not None, "redaction line changed")
    claim_texts = " ".join(str(c["claim_text"]) for c in forged.claims())
    for needle in ("placed an order", "1042", "24.00"):
        require(needle in claim_texts, f"no forged claim mentions {needle!r}")
    require("contains 'order' with status 2xx" in reasons, "order predicate changed")
    require("contains '1042'" in reasons, "1042 predicate changed")
    checkers: dict[str, str] = {}
    for c in forged.claims():
        name = str(c["checker"])
        status = str(c["status"])
        if checkers.get(name) != "contradicted":
            checkers[name] = status
    return {
        "requests": int(requests.group(1)) if requests else 0,
        "states": int(states.group(1)) if states else 0,
        "redacted": (redacted.group(2), int(redacted.group(3))) if redacted else ("", 0),
        "checkers": checkers,
        "quote": str(forged.claims()[0]["quote"]),
        "n_claims": len(forged.claims()),
        "n_key": sum(1 for c in forged.claims() if c["key"]),
    }


def render_how_it_works(forged: JudgeRun) -> str:
    facts = pipeline_facts(forged)
    width, height = 1080.0, 404.0
    cycle = 13.0
    tl = Timeline(cycle)
    out_at = cycle - 0.6
    out: list[str] = [card(width, height)]

    out.append(
        f'<text class="kick" x="32" y="44" style="fill:{CYAN}">PIPELINE</text>'
        '<text x="32" y="76" class="b" style="font-size:25px;letter-spacing:-.01em">'
        "How a verdict is reached</text>"
        '<text x="32" y="101" class="i2" style="font-size:14px">Claims come from the agent. '
        "Proof comes from the recording.</text>"
    )
    quote = f"“{facts['quote']}”"
    bw = max(sans_w(quote, 15) + 44, 300)
    bx = width - 32 - bw
    bubble = tl.appear(0.2, fade=0.4, end=out_at)
    out.append(
        f'<g class="{bubble}">'
        + glass(bx, 26, bw, 66, rx=14, extra=f' style="stroke:{VIOLET};stroke-opacity:.45"')
        + f'<text class="kick" x="{num(bx + 20)}" y="49" style="fill:{VIOLET_SOFT};font-size:10px">'
        "AGENT SAYS</text>"
        f'<text x="{num(bx + 20)}" y="75" class="it" style="font-size:15px">{esc(quote)}</text></g>'
    )

    stages = [
        Stage(1, "Ingest", ("trace · HAR", "screenshots"), 1.0),
        Stage(2, "Redact", ("secrets & PII", "→ HMAC tokens"), 2.4),
        Stage(3, "Extract claims", ("atomic, quoted,", "key-flagged"), 3.8),
        Stage(4, "Predicates", ("what would prove", "each claim"), 5.2),
        Stage(5, "Checkers", ("deterministic first,", "vision last"), 6.6),
        Stage(6, "Verdict", ("evidence-cited,", "exit code for CI"), 8.4),
    ]
    nx0, ny, nw, nh, ngap = 32.0, 150.0, 150.0, 222.0, 23.6
    xs = [nx0 + k * (nw + ngap) for k in range(len(stages))]

    rail_y = 128.0
    out.append(
        f'<line x1="{num(xs[0] + nw / 2)}" x2="{num(xs[-1] + nw / 2)}" y1="{num(rail_y)}" '
        f'y2="{num(rail_y)}" stroke="#fff" stroke-opacity=".08" stroke-dasharray="2 6"/>'
    )
    for a in xs[:-1]:
        y = ny + nh / 2
        x1, x2 = a + nw + 3, a + nw + ngap - 3
        out.append(
            f'<line x1="{num(x1)}" x2="{num(x2)}" y1="{num(y)}" y2="{num(y)}" stroke="{VIOLET}" '
            'stroke-opacity=".35" stroke-width="1.5"/>'
            f'<line class="flow" x1="{num(x1)}" x2="{num(x2)}" y1="{num(y)}" y2="{num(y)}" '
            f'stroke="{CYAN}" stroke-width="1.8" stroke-dasharray="2 5" stroke-linecap="round"/>'
            f'<circle cx="{num(x2)}" cy="{num(y)}" r="2.4" fill="{CYAN}" filter="url(#soft)"/>'
        )

    def reveal(t: float, inner: str) -> str:
        return f'<g class="{tl.appear(t, fade=0.25, end=out_at)}">{inner}</g>'

    for st, x in zip(stages, xs, strict=True):
        accent = ROSE if st.n == 6 else VIOLET
        out.append(glass(x, ny, nw, nh, rx=14, alpha=0.04))
        settle = "1" if st.n == 6 else ".5"
        lit = tl.keyframes(
            [
                (0, "opacity:0"),
                (st.start, "opacity:0"),
                (st.start + 0.2, "opacity:1"),
                (st.start + 1.3, "opacity:1"),
                (st.start + 1.6, f"opacity:{settle}"),
                (out_at, f"opacity:{settle}"),
                (out_at + 0.4, "opacity:0"),
                (cycle, "opacity:0"),
            ]
        )
        out.append(
            f'<rect class="{lit}" x="{num(x)}" y="{num(ny)}" width="{num(nw)}" height="{num(nh)}" '
            f'rx="14" fill="{accent}" fill-opacity=".07" stroke="{accent}" stroke-width="1.6" '
            f'filter="url(#soft)" style="opacity:{settle}"/>'
        )
        out.append(
            f'<circle cx="{num(x + 24)}" cy="{num(ny + 27)}" r="12" fill="{accent}" '
            'fill-opacity=".18" stroke="' + accent + '" stroke-opacity=".8"/>'
            f'<text class="m b" x="{num(x + 24)}" y="{num(ny + 31.5)}" text-anchor="middle" '
            f'style="font-size:12px;fill:#fff">{st.n}</text>'
            f'<text x="{num(x + 44)}" y="{num(ny + 32)}" class="b" style="font-size:14.5px">'
            f"{esc(st.title)}</text>"
            f'<text class="m i2" x="{num(x + 14)}" y="{num(ny + 60)}" style="font-size:10.5px">'
            f"{esc(st.desc[0])}</text>"
            f'<text class="m i2" x="{num(x + 14)}" y="{num(ny + 75)}" style="font-size:10.5px">'
            f"{esc(st.desc[1])}</text>"
            f'<line x1="{num(x + 14)}" x2="{num(x + nw - 14)}" y1="{num(ny + 90)}" '
            f'y2="{num(ny + 90)}" stroke="#fff" stroke-opacity=".07"/>'
        )

    s1, s2, s3, s4, s5, s6 = stages
    x1, x2, x3, x4, x5, x6 = xs
    cy0 = ny + 104
    ingest = [
        f"net#1{DASH}{facts['requests']}",
        f"snap#1{DASH}{facts['states']}",
        "final",
    ]
    for k, label in enumerate(ingest):
        chip_svg, _ = pill(x1 + 14, cy0 + k * 30, label, CYAN, size=10.5)
        out.append(reveal(s1.start + 0.2 + k * 0.18, chip_svg))

    kind, count = facts["redacted"]
    raw_svg, raw_w = pill(x2 + 14, cy0, f"{kind} {TIMES}{count}", ROSE, size=10.5)
    out.append(
        reveal(
            s2.start + 0.2,
            raw_svg + f'<line x1="{num(x2 + 20)}" x2="{num(x2 + 8 + raw_w)}" y1="{num(cy0 + 11)}" '
            f'y2="{num(cy0 + 11)}" stroke="{ROSE}" stroke-width="1.4"/>',
        )
    )
    tok_svg, _ = pill(x2 + 14, cy0 + 30, "[redacted:…]", EMERALD, size=10.5)
    out.append(reveal(s2.start + 0.55, tok_svg))

    claim_chips = ["placed an order", "order #1042 shown", "price 24.00"]
    for k, label in enumerate(claim_chips):
        chip_svg, _ = pill(x3 + 14, cy0 + k * 30, label, VIOLET_SOFT, size=10.5)
        out.append(reveal(s3.start + 0.25 + k * 0.2, chip_svg))
    out.append(
        reveal(
            s3.start + 0.9,
            f'<text class="m mu" x="{num(x3 + 14)}" y="{num(ny + nh - 14)}" style="font-size:10px">'
            f"{facts['n_claims']} claims · {facts['n_key']} key</text>",
        )
    )

    for k, label in enumerate(["POST ~order → 2xx", "text ∋ '1042'"]):
        chip_svg, _ = pill(x4 + 14, cy0 + k * 30, label, INK2, size=10.5, fill_opacity=0.06)
        out.append(reveal(s4.start + 0.25 + k * 0.25, chip_svg))

    checkers: dict[str, str] = facts["checkers"]
    for k, (name, status) in enumerate(checkers.items()):
        glyph, color = (
            ("✗", ROSE)
            if status == "contradicted"
            else ("✓", EMERALD)
            if status == "supported"
            else ("?", AMBER)
        )
        y = cy0 + k * 30
        out.append(
            reveal(
                s5.start + 0.3 + k * 0.35,
                f'<rect x="{num(x5 + 14)}" y="{num(y)}" width="{num(nw - 28)}" height="22" rx="7" '
                f'fill="{color}" fill-opacity=".1" stroke="{color}" stroke-opacity=".5"/>'
                f'<text class="m" x="{num(x5 + 36)}" y="{num(y + 15)}" style="font-size:9.5px;'
                f'fill:{INK}">{esc(name)}</text>'
                f'<text class="m b" x="{num(x5 + 22)}" y="{num(y + 15.5)}" '
                f'style="font-size:12px;fill:{color}">{glyph}</text>',
            )
        )

    stamp_t = s6.start + 0.3
    sx, sy = x6 + nw / 2, cy0 + 26
    slam = tl.keyframes(
        [
            (0, "opacity:0;transform:scale(2.2)"),
            (
                stamp_t,
                "opacity:0;transform:scale(2.2);animation-timing-function:cubic-bezier(.6,0,.9,.4)",
            ),
            (stamp_t + 0.2, "opacity:1;transform:scale(.94)"),
            (stamp_t + 0.34, "opacity:1;transform:scale(1.04)"),
            (stamp_t + 0.46, "opacity:1;transform:scale(1)"),
            (out_at, "opacity:1;transform:scale(1)"),
            (out_at + 0.4, "opacity:0;transform:scale(1)"),
            (cycle, "opacity:0"),
        ]
    )
    out.append(
        f'<g transform="translate({num(sx)} {num(sy)}) rotate(-6)">'
        f'<g class="{slam}" style="transform-box:view-box;transform-origin:0 0">'
        f'<rect x="-54" y="-21" width="108" height="42" rx="8" fill="{ROSE}" fill-opacity=".14" '
        f'stroke="{ROSE}" stroke-width="2.5" filter="url(#soft)"/>'
        f'<text x="0" y="9" text-anchor="middle" class="xb" style="font-size:25px;'
        f'letter-spacing:.14em;fill:{ROSE}">{esc(forged.outcome)}</text></g></g>'
    )
    out.append(
        reveal(
            stamp_t + 0.6,
            f'<text class="m" x="{num(sx)}" y="{num(cy0 + 76)}" text-anchor="middle" '
            f'style="font-size:10.5px;fill:{INK}">no <tspan fill="{ROSE}">POST /api/orders</tspan></text>'
            f'<text class="m mu" x="{num(sx)}" y="{num(cy0 + 94)}" text-anchor="middle" '
            f'style="font-size:10.5px">{esc(forged.score)} · exit {forged.exit_code}</text>',
        )
    )

    token = "“order #1042 confirmed”"
    tw = sans_w(token, 11.5) + 24
    path = [(0.4, bx + bw / 2, 96.0)]
    for st, x in zip(stages, xs, strict=True):
        path.append((st.start, x + nw / 2, rail_y))
    frames: list[tuple[float, str]] = [
        (0, f"opacity:0;transform:translate({num(path[0][1])}px,{num(path[0][2])}px)")
    ]
    ease = "animation-timing-function:cubic-bezier(.5,0,.25,1)"
    for k, (t, x, y) in enumerate(path):
        frames.append(
            (t, f"opacity:{0 if k == 0 else 1};transform:translate({num(x)}px,{num(y)}px);{ease}")
        )
        if 0 < k < len(path) - 1:
            hold_t = min(t + 0.6, path[k + 1][0] - 0.05)
            frames.append((hold_t, f"opacity:1;transform:translate({num(x)}px,{num(y)}px);{ease}"))
    lx, ly = path[-1][1], path[-1][2]
    frames.append((stamp_t + 0.15, f"opacity:0;transform:translate({num(lx)}px,{num(ly + 40)}px)"))
    frames.append((cycle, f"opacity:0;transform:translate({num(lx)}px,{num(ly + 40)}px)"))
    out.append(
        f'<g class="{tl.keyframes(frames)}" style="opacity:0">'
        f'<rect x="{num(-tw / 2)}" y="-12" width="{num(tw)}" height="24" rx="12" fill="{VIOLET}" '
        'filter="url(#soft)"/>'
        f'<text x="0" y="4" text-anchor="middle" class="it sb" style="font-size:11.5px;fill:#fff">'
        f"{esc(token)}</text></g>"
    )

    style = (
        base_style()
        + "@keyframes flow{to{stroke-dashoffset:-14}}.flow{animation:flow .9s linear infinite}"
        + tl.css()
        + REDUCED_MOTION
    )
    title = (
        "How Evidence Judge works: ingest, redact, extract claims, predicates, checkers, verdict"
    )
    return svg_doc(width, height, title, style, "".join(out))


@dataclass(frozen=True)
class BenchRow:
    judge: str
    kind: str
    n: int
    false_pass: int
    review: int
    fail: int
    errors: int


def bench_rows(data: dict[str, Any]) -> list[BenchRow]:
    return [
        BenchRow(
            judge=m["judge"],
            kind=m["kind"],
            n=m["false_pass"]["n"],
            false_pass=m["false_pass"]["k"],
            review=m["needs_review"],
            fail=m["fail"],
            errors=m["errors"],
        )
        for m in data["forgery_metrics"]
    ]


JUDGE_LABELS = {
    "evidence-judge+spec": ("exact spec", "Evidence Judge · exact spec"),
    "evidence-judge+generic-spec": ("generic", "Evidence Judge · generic spec"),
    "evidence-judge": ("no spec", "Evidence Judge · no spec"),
}
POOLED = "all manipulations"
CONTROL = "style_only"


def stacked_bar(
    x: float, y: float, w: float, h: float, row: BenchRow, cls: str, rules: list[str], delay: float
) -> str:
    segs = [(row.fail, CYAN), (row.review, AMBER), (row.false_pass, ROSE)]
    parts = [
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="{num(h / 2)}" '
        'fill="#fff" fill-opacity=".06"/>'
    ]
    bars: list[str] = []
    sx = x
    for count, color in segs:
        if not count or not row.n:
            continue
        sw = w * count / row.n
        gap = 2.0 if sx + sw < x + w - 0.5 else 0.0
        bars.append(
            f'<rect x="{num(sx)}" y="{num(y)}" width="{num(max(sw - gap, 1))}" height="{num(h)}" '
            f'rx="{num(min(h / 2, 3))}" fill="{color}"/>'
        )
        sx += sw
    rules.append(
        f".{cls}{{transform-box:view-box;transform-origin:{num(x)}px 0;"
        f"animation:grow 1s cubic-bezier(.2,.8,.2,1) {num(delay)}s both}}"
    )
    parts.append(f'<g class="{cls}">{"".join(bars)}</g>')
    return "".join(parts)


def render_benchmark(data: dict[str, Any]) -> str:
    rows = bench_rows(data)
    by_key = {(r.judge, r.kind): r for r in rows}
    judges: list[str] = list(data["judges"])
    for j in judges:
        require(j in JUDGE_LABELS, f"unknown judge {j}")
    kinds: list[str] = list(data["kinds"])
    manip = [k for k in kinds if k != CONTROL]
    pooled = [by_key[(j, POOLED)] for j in judges]
    seeds = int(data["seeds"])
    forged_scenarios = sum(1 for s in data["scenarios"] if s["forged"])
    for p in pooled:
        require(p.n == forged_scenarios * len(manip) * seeds, "pooled n no longer adds up")
        require(sum(by_key[(p.judge, k)].n for k in manip) == p.n, "per-kind n mismatch")
    worst = max(pooled, key=lambda r: r.false_pass)
    ci_high = max(
        float(m["false_pass"]["ci_high"]) for m in data["forgery_metrics"] if m["kind"] == POOLED
    )

    width = 1080.0
    top = 150.0
    gx, gw = 436.0, width - 32 - 436
    cols, gap = 3, 14.0
    cw = (gw - gap * (cols - 1)) / cols
    ch = 150.0
    grid_rows = math.ceil(len(kinds) / cols)
    panel_h = grid_rows * ch + (grid_rows - 1) * gap
    height = top + panel_h + 64
    rules: list[str] = []
    out: list[str] = [card(width, height)]

    out.append(
        f'<text class="kick" x="32" y="46" style="fill:{CYAN}">BENCHMARK · FORGED SUCCESS '
        "STORIES</text>"
        '<text x="32" y="78" class="b" style="font-size:25px;letter-spacing:-.01em">'
        "Can a forged success story fool the judge?</text>"
        '<text x="32" y="103" class="i2" style="font-size:14px">Failed runs rewritten to claim '
        "success, evidence untouched. Any PASS is a miss.</text>"
        f'<text class="m mu" x="32" y="126" style="font-size:11px">{forged_scenarios} failed runs '
        f"{TIMES} {len(manip)} forgery kinds {TIMES} {seeds} seeds = {worst.n} per judge · "
        "offline · bench/results/offline-fixtures.json</text>"
    )

    lx, lw = 32.0, 386.0
    out.append(glass(lx, top, lw, panel_h, rx=16, alpha=0.03))
    head = f"{worst.false_pass} / {worst.n}"
    out.append(
        f'<text x="{num(lx + 24)}" y="{num(top + 70)}" class="xb" style="font-size:60px;'
        f'letter-spacing:-.02em;fill:{EMERALD}" filter="url(#soft)">{esc(head)}</text>'
        f'<text x="{num(lx + 24)}" y="{num(top + 98)}" class="sb" style="font-size:16px">'
        "forged runs passed</text>"
        f'<text class="m mu" x="{num(lx + 24)}" y="{num(top + 118)}" style="font-size:11px">'
        f"for every judge · 95% CI 0{DASH}{round(ci_high * 100)}%</text>"
    )
    by = top + 146
    seq = 0
    for p in pooled:
        long_label = JUDGE_LABELS[p.judge][1]
        out.append(
            f'<text x="{num(lx + 24)}" y="{num(by)}" style="font-size:12.5px">{esc(long_label)}</text>'
            f'<text class="m" x="{num(lx + lw - 24)}" y="{num(by)}" text-anchor="end" '
            f'style="font-size:11px"><tspan fill="{CYAN}">{p.fail} FAIL</tspan>'
            f'<tspan fill="{MUTED}"> · </tspan><tspan fill="{AMBER}">{p.review} review</tspan>'
            "</text>"
        )
        out.append(stacked_bar(lx + 24, by + 9, lw - 48, 9, p, f"g{seq}", rules, 0.2 + seq * 0.12))
        seq += 1
        by += 44
    out.append(
        f'<text x="{num(lx + 24)}" y="{num(by)}" class="i2" style="font-size:12.5px">'
        "Typical LLM judge</text>"
        f'<text class="m" x="{num(lx + lw - 24)}" y="{num(by)}" text-anchor="end" '
        f'style="font-size:11px;fill:{MUTED}">not yet measured</text>'
        f'<rect x="{num(lx + 24)}" y="{num(by + 9)}" width="{num(lw - 48)}" height="9" rx="4.5" '
        f'fill="none" stroke="{MUTED}" stroke-dasharray="4 3"/>'
    )

    for k, kind in enumerate(kinds):
        r, c = divmod(k, cols)
        x = gx + c * (cw + gap)
        y = top + r * (ch + gap)
        control = kind == CONTROL
        out.append(glass(x, y, cw, ch, rx=14, alpha=0.03))
        out.append(
            f'<text class="m b" x="{num(x + 16)}" y="{num(y + 26)}" style="font-size:12px">'
            f"{esc(kind)}</text>"
        )
        if control:
            out.append(
                f'<text class="m" x="{num(x + cw - 16)}" y="{num(y + 26)}" text-anchor="end" '
                f'style="font-size:9.5px;fill:{AMBER}">CONTROL</text>'
            )
        ry = y + 48
        total_pass = 0
        total_n = 0
        for j in judges:
            row = by_key[(j, kind)]
            total_pass += row.false_pass
            total_n += row.n
            out.append(
                f'<text class="m mu" x="{num(x + 16)}" y="{num(ry + 8)}" style="font-size:10px">'
                f"{esc(JUDGE_LABELS[j][0])}</text>"
            )
            bar_x = x + 86
            bar_w = cw - 86 - 52
            out.append(stacked_bar(bar_x, ry, bar_w, 8, row, f"g{seq}", rules, 0.5 + seq * 0.05))
            out.append(
                f'<text class="m" x="{num(x + cw - 16)}" y="{num(ry + 8)}" text-anchor="end" '
                f'style="font-size:10px"><tspan fill="{CYAN}">{row.fail}</tspan>'
                f'<tspan fill="{MUTED}">/</tspan><tspan fill="{AMBER}">{row.review}</tspan></text>'
            )
            seq += 1
            ry += 24
        note = f"{len(judges)} judges"
        out.append(
            f'<text class="m" x="{num(x + 16)}" y="{num(y + ch - 16)}" style="font-size:10.5px">'
            f'<tspan fill="{EMERALD if total_pass == 0 else ROSE}" font-weight="700">'
            f"{total_pass}/{total_n} passed</tspan>"
            f'<tspan fill="{MUTED}"> · {esc(note)}</tspan></text>'
        )

    ly = height - 30
    items = [
        ("FAIL · caught", CYAN, False),
        ("NEEDS_REVIEW · escalated", AMBER, False),
        ("PASS · fooled", ROSE, False),
        ("not yet measured", MUTED, True),
    ]
    lgx = 32.0
    for label, color, dashed in items:
        sw = (
            f'<rect x="{num(lgx)}" y="{num(ly - 8)}" width="18" height="9" rx="4.5" fill="none" '
            f'stroke="{color}" stroke-dasharray="3 2"/>'
            if dashed
            else f'<rect x="{num(lgx)}" y="{num(ly - 8)}" width="18" height="9" rx="4.5" fill="{color}"/>'
        )
        out.append(
            sw + f'<text class="m i2" x="{num(lgx + 26)}" y="{num(ly)}" style="font-size:11px">'
            f"{esc(label)}</text>"
        )
        lgx += 26 + mono_w(label, 11) + 26
    out.append(
        f'<text class="m mu" x="{num(width - 32)}" y="{num(ly)}" text-anchor="end" '
        'style="font-size:11px">per kind: FAIL/review counts</text>'
    )

    style = (
        base_style()
        + "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        + "".join(rules)
        + REDUCED_MOTION
    )
    title = f"Forged-run benchmark: {worst.false_pass} of {worst.n} forged runs passed"
    return svg_doc(width, height, title, style, "".join(out))


def render_mascot() -> str:
    head_hi, head_lo = "#262b52", "#121530"
    wig, wig_line = "#eeeaff", "#b9a8f7"
    defs = (
        "<defs>"
        '<radialGradient id="halo" cx="50%" cy="50%" r="50%">'
        f'<stop offset="0" stop-color="{VIOLET}" stop-opacity=".28"/>'
        f'<stop offset=".6" stop-color="{VIOLET}" stop-opacity=".08"/>'
        f'<stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="head" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{head_hi}"/>'
        f'<stop offset="1" stop-color="{head_lo}"/></linearGradient>'
        '<linearGradient id="body" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b1f40"/>'
        '<stop offset="1" stop-color="#0c0e22"/></linearGradient>'
        '<radialGradient id="iris" cx="50%" cy="45%" r="55%">'
        '<stop offset="0" stop-color="#a5f3fc"/><stop offset=".45" stop-color="#22d3ee"/>'
        '<stop offset="1" stop-color="#0e7490"/></radialGradient>'
        '<radialGradient id="lensg" cx="40%" cy="35%" r="70%">'
        f'<stop offset="0" stop-color="{CYAN}" stop-opacity=".35"/>'
        f'<stop offset="1" stop-color="{CYAN}" stop-opacity=".08"/></radialGradient>'
        '<clipPath id="lens"><circle cx="152" cy="140" r="17"/></clipPath>'
        f"{GLOW_FILTERS}</defs>"
    )
    curls = []
    for side in (-1, 1):
        for k in range(3):
            cx = 100 + side * (57 + k * 1.5)
            cy = 74 + k * 13
            curls.append(
                f'<rect x="{num(cx - 9)}" y="{num(cy - 6)}" width="18" height="12" rx="6" '
                f'fill="{wig}" stroke="{wig_line}" stroke-width="1.2"/>'
            )

    def eye(cx: float) -> str:
        return (
            f'<circle cx="{num(cx)}" cy="86" r="21" fill="#090b1c" stroke="{CYAN}" '
            'stroke-opacity=".35" stroke-width="1.2"/>'
            f'<circle class="glo" cx="{num(cx)}" cy="86" r="13" fill="url(#iris)" filter="url(#glow)"/>'
            f'<circle cx="{num(cx)}" cy="86" r="5.6" fill="#03121a"/>'
            f'<circle cx="{num(cx + 4)}" cy="81.5" r="2.4" fill="#fff"/>'
            f'<rect class="lid" x="{num(cx - 15)}" y="71" width="30" height="30" rx="15" '
            'fill="url(#head)"/>'
        )

    body = (
        defs
        + '<circle cx="100" cy="104" r="92" fill="url(#halo)"/>'
        + '<ellipse cx="100" cy="186" rx="46" ry="5" fill="#000" fill-opacity=".18"/>'
        + f'<path d="M50 120C50 92 150 92 150 120L146 160C140 182 60 182 54 160Z" fill="url(#body)" '
        f'stroke="{VIOLET}" stroke-opacity=".55" stroke-width="1.4"/>'
        + f'<path d="M74 126L100 168L126 126" fill="none" stroke="{VIOLET}" stroke-opacity=".6" '
        'stroke-width="1.6"/>'
        + f'<rect x="93" y="128" width="6.5" height="22" rx="2" fill="{wig}"/>'
        + f'<rect x="100.5" y="128" width="6.5" height="22" rx="2" fill="{wig}"/>'
        + '<path d="M52 128C40 140 42 162 56 170L64 140Z" fill="#161a36" stroke="'
        + VIOLET
        + '" stroke-opacity=".4"/>'
        + f'<path d="M84 180l-5 7M90 181v7M110 181v7M116 180l5 7" stroke="{AMBER}" '
        'stroke-width="3" stroke-linecap="round"/>'
        + '<g class="head">'
        + f'<path d="M52 66L40 26L76 50Z" fill="#191d3c" stroke="{VIOLET}" stroke-opacity=".6" '
        'stroke-width="1.2"/>'
        + f'<path d="M148 66L160 26L124 50Z" fill="#191d3c" stroke="{VIOLET}" stroke-opacity=".6" '
        'stroke-width="1.2"/>'
        + f'<path d="M44 92C44 56 70 40 100 40C130 40 156 56 156 92C156 120 132 132 100 132'
        f'C68 132 44 120 44 92Z" fill="url(#head)" stroke="{VIOLET}" stroke-opacity=".7" '
        'stroke-width="1.5"/>'
        + f'<path d="M50 74C50 48 72 34 100 34C128 34 150 48 150 74C136 62 120 58 100 58'
        f'C80 58 64 62 50 74Z" fill="{wig}" stroke="{wig_line}" stroke-width="1.2"/>'
        + f'<path d="M64 52C76 46 88 44 100 44C112 44 124 46 136 52" fill="none" '
        f'stroke="{wig_line}" stroke-width="1.2"/>'
        + "".join(curls)
        + eye(78)
        + eye(122)
        + f'<path d="M100 100L107 109L100 120L93 109Z" fill="{AMBER}" stroke="#b45309" '
        'stroke-width="1"/>'
        + "</g>"
        + '<line x1="164" y1="153" x2="180" y2="172" stroke="#94a3b8" stroke-width="6.5" '
        'stroke-linecap="round"/>'
        + '<line x1="164" y1="153" x2="180" y2="172" stroke="#cbd5e1" stroke-width="2" '
        'stroke-linecap="round" stroke-opacity=".6"/>'
        + f'<circle cx="152" cy="140" r="24" fill="{CYAN}" fill-opacity=".12" filter="url(#blur)"/>'
        + '<circle cx="152" cy="140" r="17" fill="url(#lensg)"/>'
        + '<g clip-path="url(#lens)"><rect class="glint" x="146" y="112" width="7" height="56" '
        'fill="#fff" opacity=".8"/></g>'
        + '<circle cx="152" cy="140" r="17" fill="none" stroke="#cbd5e1" stroke-width="4"/>'
        + f'<circle cx="152" cy="140" r="19.6" fill="none" stroke="{CYAN}" stroke-opacity=".55" '
        'stroke-width="1"/>'
        + f'<path d="M134 168C146 170 156 162 158 152C150 150 140 154 136 160Z" fill="#1d2146" '
        f'stroke="{VIOLET}" stroke-opacity=".5"/>'
    )
    style = (
        ".head{transform-box:view-box;transform-origin:100px 132px;"
        "animation:tilt 8s ease-in-out infinite}"
        "@keyframes tilt{0%,100%{transform:rotate(-2deg)}50%{transform:rotate(2.5deg)}}"
        ".lid{transform-box:fill-box;transform-origin:center;transform:scaleY(0);"
        "animation:blink 5s infinite}"
        "@keyframes blink{0%,91%,100%{transform:scaleY(0)}94%,96%{transform:scaleY(1)}}"
        ".glo{animation:glo 3.2s ease-in-out infinite}"
        "@keyframes glo{0%,100%{opacity:1}50%{opacity:.82}}"
        ".glint{transform-box:view-box;transform-origin:152px 140px;"
        "transform:translate(-30px,0) rotate(28deg);animation:glint 4s ease-in-out infinite}"
        "@keyframes glint{0%,55%{transform:translate(-30px,0) rotate(28deg)}"
        "80%,100%{transform:translate(30px,0) rotate(28deg)}}" + REDUCED_MOTION
    )
    return svg_doc(200, 200, "Judge Owl, the Evidence Judge mascot", style, body)


def build() -> dict[str, str]:
    forged = run_judge("forged_success")
    honest = run_judge("honest_success")
    bench: dict[str, Any] = json.loads(BENCH_RESULTS.read_text(encoding="utf-8"))
    return {
        "hero.svg": render_hero(forged, bench),
        "mascot.svg": render_mascot(),
        "demo-forged.svg": render_terminal(
            forged, "evidence-judge judge on a forged success run: FAIL, exit code 1", hold=5.0
        ),
        "demo-honest.svg": render_terminal(
            honest, "evidence-judge judge on an honest success run: PASS, exit code 0", hold=4.5
        ),
        "how-it-works.svg": render_how_it_works(forged),
        "benchmark.svg": render_benchmark(bench),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the animated README SVGs in docs/assets."
    )
    parser.add_argument("--check", action="store_true", help="fail if docs/assets is out of date")
    args = parser.parse_args(argv)
    assets = build()
    stale = [
        n
        for n, s in assets.items()
        if not (ASSETS / n).is_file() or (ASSETS / n).read_text(encoding="utf-8") != s
    ]
    unexpected = sorted(p.name for p in ASSETS.glob("*.svg") if p.name not in assets)
    if args.check:
        for name in stale:
            print(f"out of date: docs/assets/{name}")
        for name in unexpected:
            print(f"not generated: docs/assets/{name}")
        return 1 if stale or unexpected else 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name in unexpected:
        (ASSETS / name).unlink()
        print(f"removed docs/assets/{name}")
    for name, svg in assets.items():
        (ASSETS / name).write_text(svg, encoding="utf-8")
        print(f"wrote docs/assets/{name} ({len(svg.encode()) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
