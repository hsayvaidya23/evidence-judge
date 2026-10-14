from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import re
import subprocess
import sys
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
DASH = "\u2013"
TIMES = "\u00d7"
REDUCED_MOTION = "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"

TERM_COLUMNS = 110
FONT_SIZE = 12.5
CW = 7.5
LH = 17.0
PAD_X = 22.0
CHROME_H = 34.0
MARGIN = 14.0

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")

TERM_FG = {
    None: "#e6edf3",
    "black": "#6e7681",
    "red": "#ff7b72",
    "green": "#56d364",
    "yellow": "#e3b341",
    "blue": "#79c0ff",
    "magenta": "#d2a8ff",
    "cyan": "#56d4dd",
    "white": "#ffffff",
}
TERM_BG = {"red": "#da3633", "green": "#238636", "yellow": "#9e6a03", "blue": "#1f6feb"}
TERM_DIM = "#8b949e"
TERM_SURFACE = "#0d1117"


def esc(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in {"-0", ""} else "0"


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

    def appear(self, start: float, fade: float = 0.12, end: float | None = None) -> str:
        stop = self.cycle - 0.45 if end is None else end
        return self.keyframes(
            [
                (0, "opacity:0"),
                (start, "opacity:0"),
                (start + fade, "opacity:1"),
                (stop, "opacity:1"),
                (min(stop + 0.3, self.cycle), "opacity:0"),
                (self.cycle, "opacity:0"),
            ]
        )

    def css(self) -> str:
        return "".join(self.rules)


@dataclass(frozen=True)
class Style:
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False


@dataclass(frozen=True)
class Span:
    col: int
    text: str
    style: Style


def apply_sgr(style: Style, params: str) -> Style:
    codes = [int(p) for p in params.split(";") if p] or [0]
    i = 0
    while i < len(codes):
        code = codes[i]
        if code == 0:
            style = Style()
        elif code == 1:
            style = dataclasses.replace(style, bold=True)
        elif code == 2:
            style = dataclasses.replace(style, dim=True)
        elif code == 3:
            style = dataclasses.replace(style, italic=True)
        elif code == 22:
            style = dataclasses.replace(style, bold=False, dim=False)
        elif code == 23:
            style = dataclasses.replace(style, italic=False)
        elif 30 <= code <= 37:
            style = dataclasses.replace(style, fg=ANSI_NAMES[code - 30])
        elif 90 <= code <= 97:
            style = dataclasses.replace(style, fg=ANSI_NAMES[code - 90])
        elif code == 39:
            style = dataclasses.replace(style, fg=None)
        elif 40 <= code <= 47:
            style = dataclasses.replace(style, bg=ANSI_NAMES[code - 40])
        elif code == 49:
            style = dataclasses.replace(style, bg=None)
        elif code in (38, 48):
            i += 2 if i + 1 < len(codes) and codes[i + 1] == 5 else 4
        i += 1
    return style


def parse_ansi_line(line: str, style: Style | None = None) -> tuple[list[Span], Style]:
    current = style or Style()
    spans: list[Span] = []
    col = 0
    pos = 0
    for match in ANSI_RE.finditer(line):
        text = line[pos : match.start()]
        if text:
            spans.append(Span(col, text, current))
            col += len(text)
        current = apply_sgr(current, match.group(1))
        pos = match.end()
    if line[pos:]:
        spans.append(Span(col, line[pos:], current))
    return spans, current


def parse_ansi(text: str) -> list[list[Span]]:
    lines: list[list[Span]] = []
    style = Style()
    for raw in text.rstrip("\n").split("\n"):
        spans, style = parse_ansi_line(raw, style)
        lines.append(spans)
    return lines


def plain(spans: Sequence[Span]) -> str:
    out = ""
    for span in spans:
        out = out.ljust(span.col) + span.text
    return out


@dataclass(frozen=True)
class CliRun:
    scenario: str
    lines: list[list[Span]]
    exit_code: int


def run_judge(scenario: str, columns: int = TERM_COLUMNS) -> CliRun:
    d = RUNS / scenario
    env = {k: v for k, v in os.environ.items() if k not in {"NO_COLOR", "ANTHROPIC_API_KEY"}}
    env.update({"FORCE_COLOR": "1", "COLUMNS": str(columns), "TERM": "xterm-256color"})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "evidence_judge",
            "judge",
            "--trace",
            str(d / "trace.zip"),
            "--har",
            str(d / "run.har"),
            "--answer-file",
            str(d / "answer.txt"),
            "--spec",
            str(d / "spec.yaml"),
            "--offline",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode not in (0, 1, 2):
        raise SystemExit(f"evidence-judge exited {proc.returncode} on {scenario}:\n{proc.stdout}")
    return CliRun(scenario, parse_ansi(proc.stdout), proc.returncode)


ITEM_MARKER = re.compile(r"^\s+[x?!] $")


def is_item_head(spans: Sequence[Span]) -> bool:
    return bool(spans) and not spans[0].style.dim and bool(ITEM_MARKER.match(spans[0].text))


def is_section(spans: Sequence[Span]) -> bool:
    if not spans or spans[0].col != 0 or spans[0].text.startswith(" "):
        return False
    return spans[0].style.bold or plain(spans).startswith("llm calls")


def truncate_item(spans: Sequence[Span]) -> list[Span]:
    kept: list[Span] = []
    for span in spans:
        if span.style.dim and kept:
            break
        kept.append(span)
    tail = kept[-1]
    return [*kept[:-1], Span(tail.col, tail.text.rstrip() + " …", tail.style)]


def fold_details(lines: list[list[Span]]) -> list[list[Span]]:
    end = next((i for i, ln in enumerate(lines) if plain(ln).startswith("└")), None)
    if end is None:
        return lines
    out = lines[: end + 1]
    folded = 0
    in_item = False
    for spans in [*lines[end + 1 :], []]:
        head, section = is_item_head(spans), is_section(spans) or not spans
        if head or section:
            if folded:
                out.append(
                    [Span(2, f"⋯ {folded} more lines of detail", Style(dim=True, italic=True))]
                )
                folded = 0
            in_item = head
            if spans:
                out.append(truncate_item(spans) if head else spans)
        elif not in_item:
            folded += 1
    return out


SCORE_RE = re.compile(r"score \d+/\d+")


def score_of(run: CliRun) -> str:
    for spans in run.lines:
        found = SCORE_RE.search(plain(spans))
        if found:
            return found.group(0)
    raise SystemExit(f"no score line in the {run.scenario} output")


@dataclass
class TermLine:
    spans: list[Span]
    appear: float
    typing: tuple[int, int, float, float] | None = None
    band: str | None = None


@dataclass
class TermScript:
    lines: list[TermLine] = field(default_factory=list)
    t: float = 0.5

    def prompt(self, cmd: str, *, cont: bool = False, rate: float = 0.024) -> None:
        mark = ">" if cont else "$"
        spans = [Span(0, mark, Style(fg="green", bold=True)), Span(2, cmd, Style())]
        start = self.t + 0.35
        stop = start + rate * len(cmd)
        self.lines.append(TermLine(spans, self.t, (2, len(cmd), start, stop)))
        self.t = stop + 0.25

    def output(self, spans: list[Span], gap: float) -> None:
        self.lines.append(TermLine(spans, self.t))
        self.t += gap


def mark_bands(lines: Sequence[TermLine]) -> None:
    current: str | None = None
    for line in lines:
        text = plain(line.spans)
        if text.startswith(("└", "┏", "┡", "┃")):
            current = None
            continue
        if not text.startswith("│"):
            current = None
            continue
        status = text[1:].split("│", 1)[0].strip()
        if status:
            current = (
                "red" if status == "contradicted" else "green" if status == "supported" else None
            )
        line.band = current
    if any(line.band == "red" for line in lines):
        for line in lines:
            if line.band == "green":
                line.band = None


def render_terminal(run: CliRun, title: str, *, hold: float) -> str:
    folded = fold_details(run.lines)
    script = TermScript()
    d = (RUNS / run.scenario).as_posix()
    script.prompt(f"d={d}")
    script.prompt("evidence-judge judge --trace $d/trace.zip --har $d/run.har \\")
    script.prompt("--answer-file $d/answer.txt --spec $d/spec.yaml --offline", cont=True)
    script.t += 0.55
    out_start = len(script.lines)
    for spans in folded:
        text = plain(spans)
        gap = 0.045 if text.startswith(("│", "┃", "┡", "┏")) else 0.075
        script.output(spans, gap)
    mark_bands(script.lines[out_start:])
    band_at = script.t + 0.25
    script.t += 0.9
    script.prompt("echo $?", rate=0.07)
    code_style = Style(fg="red" if run.exit_code else "green", bold=True)
    script.output([Span(0, str(run.exit_code), code_style)], 0.35)
    final_prompt = TermLine([Span(0, "$", Style(fg="green", bold=True))], script.t)
    script.lines.append(final_prompt)
    cycle = script.t + hold

    tl = Timeline(cycle)
    width_cols = max(TERM_COLUMNS, *(len(plain(ln.spans)) for ln in script.lines))
    card_w = PAD_X * 2 + width_cols * CW
    body_top = CHROME_H + 14
    card_h = body_top + len(script.lines) * LH + 16
    total_w = card_w + MARGIN * 2
    total_h = card_h + MARGIN * 2

    table_cols = max(
        (len(plain(ln.spans)) for ln in script.lines if plain(ln.spans).startswith("┏")),
        default=width_cols,
    )
    body: list[str] = []
    blocks: list[tuple[str, int, int]] = []
    for i, line in enumerate(script.lines):
        if line.band is None:
            continue
        if blocks and blocks[-1][0] == line.band and blocks[-1][2] == i:
            blocks[-1] = (line.band, blocks[-1][1], i + 1)
        else:
            blocks.append((line.band, i, i + 1))
    for band, first, last in blocks:
        y_top = body_top + first * LH
        h = (last - first) * LH
        color = TERM_FG["red"] if band == "red" else TERM_FG["green"]
        base, peak = (0.17, 0.42) if band == "red" else (0.11, 0.24)
        frames: list[tuple[float, str]] = [
            (0, "opacity:0"),
            (band_at, "opacity:0"),
            (band_at + 0.25, f"opacity:{peak}"),
            (band_at + 0.9, f"opacity:{base}"),
            (cycle - 0.45, f"opacity:{base}"),
            (cycle - 0.15, "opacity:0"),
            (cycle, "opacity:0"),
        ]
        x0 = PAD_X + CW / 2
        body.append(
            f'<rect class="{tl.keyframes(frames)}" x="{num(x0)}" y="{num(y_top)}" '
            f'width="{num((table_cols - 1) * CW)}" height="{num(h)}" fill="{color}" '
            f'style="opacity:{base}"/>'
        )
        edge = tl.appear(band_at, fade=0.25, end=cycle - 0.45)
        body.append(
            f'<rect class="{edge}" x="{num(x0 - 5)}" y="{num(y_top + 1)}" width="3" '
            f'height="{num(h - 2)}" rx="1.5" fill="{color}"/>'
        )
    for i, line in enumerate(script.lines):
        y_top = body_top + i * LH
        cls = tl.appear(line.appear, fade=0.05)
        body.append(f'<g class="{cls}">{render_spans(line.spans, y_top)}')
        if line.typing is not None:
            col, n, start, stop = line.typing
            x = PAD_X + col * CW
            shift = f"translateX({num(n * CW)}px)"
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
                f'<rect class="{cover}" x="{num(x)}" y="{num(y_top)}" width="{num((n + 1) * CW)}" '
                f'height="{num(LH)}" fill="{TERM_SURFACE}" style="opacity:0"/>'
            )
            nxt = script.lines[i + 1].appear if i + 1 < len(script.lines) else cycle
            cursor = tl.keyframes(
                [
                    (0, "opacity:0;transform:none"),
                    (line.appear, "opacity:0;transform:none"),
                    (line.appear + 0.01, "opacity:.85;transform:none"),
                    (start, f"opacity:.85;transform:none;animation-timing-function:steps({n},end)"),
                    (stop, f"opacity:.85;transform:{shift}"),
                    (nxt, f"opacity:.85;transform:{shift}"),
                    (nxt + 0.01, f"opacity:0;transform:{shift}"),
                    (cycle, "opacity:0"),
                ]
            )
            body.append(
                f'<rect class="{cursor}" x="{num(x)}" y="{num(y_top + 1.5)}" width="{num(CW)}" '
                f'height="{num(LH - 3)}" fill="#e6edf3" style="opacity:0"/>'
            )
        if line is final_prompt:
            body.append(
                f'<rect class="blink" x="{num(PAD_X + 2 * CW)}" y="{num(y_top + 1.5)}" '
                f'width="{num(CW)}" height="{num(LH - 3)}" fill="#e6edf3" opacity=".85"/>'
            )
        body.append("</g>")

    lights = "".join(
        f'<circle cx="{num(20 + k * 19)}" cy="{num(CHROME_H / 2)}" r="6" fill="{c}"/>'
        for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840"))
    )
    style = (
        f"text{{font-family:{MONO};font-size:{num(FONT_SIZE)}px;fill:{TERM_FG[None]};"
        "white-space:pre}"
        ".B{font-weight:700}.I{font-style:italic}.D{fill:"
        + TERM_DIM
        + "}"
        + "".join(f".c-{name}{{fill:{color}}}" for name, color in TERM_FG.items() if name)
        + "@keyframes blink{0%,49%{opacity:.85}50%,100%{opacity:0}}"
        ".blink{animation:blink 1.05s step-end infinite}" + tl.css() + REDUCED_MOTION
    )
    return svg_doc(
        total_w,
        total_h,
        title,
        style,
        f'<defs><filter id="sh" x="-5%" y="-5%" width="110%" height="115%">'
        f'<feDropShadow dx="0" dy="4" stdDeviation="5" flood-color="#000" flood-opacity=".28"/>'
        f"</filter></defs>"
        f'<g transform="translate({num(MARGIN)} {num(MARGIN)})">'
        f'<rect width="{num(card_w)}" height="{num(card_h)}" rx="10" fill="{TERM_SURFACE}" '
        f'stroke="#30363d" filter="url(#sh)"/>'
        f'<path d="M0 10a10 10 0 0 1 10-10h{num(card_w - 20)}a10 10 0 0 1 10 10v{num(CHROME_H - 10)}'
        f'h-{num(card_w)}z" fill="#161b22"/>'
        f'<line x1="0" x2="{num(card_w)}" y1="{num(CHROME_H)}" y2="{num(CHROME_H)}" stroke="#30363d"/>'
        f"{lights}"
        f'<text x="{num(card_w / 2)}" y="{num(CHROME_H / 2 + 4)}" text-anchor="middle" '
        f'style="font-family:{SANS};font-size:12px;fill:#8b949e">evidence-judge · zsh</text>'
        + "".join(body)
        + "</g>",
    )


def render_spans(spans: Sequence[Span], y_top: float) -> str:
    parts: list[str] = []
    baseline = y_top + LH * 0.74
    for span in spans:
        if span.style.bg is not None:
            parts.append(
                f'<rect x="{num(PAD_X + span.col * CW - 1)}" y="{num(y_top + 1)}" '
                f'width="{num(len(span.text) * CW + 2)}" height="{num(LH - 2)}" rx="3" '
                f'fill="{TERM_BG.get(span.style.bg, "#30363d")}"/>'
            )
    for span in spans:
        stripped = span.text.lstrip(" ")
        col = span.col + len(span.text) - len(stripped)
        stripped = stripped.rstrip(" ")
        if not stripped:
            continue
        classes: list[str] = []
        if span.style.bold:
            classes.append("B")
        if span.style.italic:
            classes.append("I")
        if span.style.fg is not None and span.style.fg in TERM_FG:
            classes.append(f"c-{span.style.fg}")
        elif span.style.dim:
            classes.append("D")
        attrs = f' class="{" ".join(classes)}"' if classes else ""
        length = f' textLength="{num(len(stripped) * CW)}" lengthAdjust="spacingAndGlyphs"'
        opacity = ' opacity=".75"' if span.style.dim and span.style.fg is not None else ""
        parts.append(
            f'<text x="{num(PAD_X + col * CW)}" y="{num(baseline)}"{attrs}{length}{opacity} '
            f'xml:space="preserve">{esc(stripped)}</text>'
        )
    return "".join(parts)


def svg_doc(width: float, height: float, title: str, style: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(width)}" height="{num(height)}" '
        f'viewBox="0 0 {num(width)} {num(height)}" role="img" aria-label="{esc(title)}">'
        f"<title>{esc(title)}</title><style>{style}</style>{body}</svg>\n"
    )


THEMES: dict[str, dict[str, str]] = {
    "light": {
        "panel": "#f6f8fa",
        "panel_line": "#d1d9e0",
        "card": "#ffffff",
        "border": "#d1d9e0",
        "text": "#1f2328",
        "text2": "#59636e",
        "muted": "#818b98",
        "accent": "#6639ba",
        "accent_soft": "#fbefff",
        "red": "#cf222e",
        "red_soft": "#ffebe9",
        "green": "#1a7f37",
        "green_soft": "#dafbe1",
        "chip": "#0969da",
        "chip_soft": "#ddf4ff",
        "track": "#e7ecf0",
        "fail_bar": "#2a78d6",
        "review_bar": "#eb6834",
        "pass_bar": "#e34948",
        "shadow": "#1f2328",
    },
    "dark": {
        "panel": "#0d1117",
        "panel_line": "#30363d",
        "card": "#151b23",
        "border": "#3d444d",
        "text": "#f0f6fc",
        "text2": "#9198a1",
        "muted": "#656c76",
        "accent": "#ab7df8",
        "accent_soft": "#271a40",
        "red": "#f85149",
        "red_soft": "#3c1618",
        "green": "#3fb950",
        "green_soft": "#12261e",
        "chip": "#4493f8",
        "chip_soft": "#0f2341",
        "track": "#212830",
        "fail_bar": "#3987e5",
        "review_bar": "#d95926",
        "pass_bar": "#e66767",
        "shadow": "#000000",
    },
}


def base_style(c: dict[str, str]) -> str:
    return (
        f"text{{font-family:{SANS};fill:{c['text']}}}"
        f".m{{font-family:{MONO}}}.t2{{fill:{c['text2']}}}.mu{{fill:{c['muted']}}}"
        ".b{font-weight:700}.sb{font-weight:600}.i{font-style:italic}"
    )


def chip_width(label: str) -> float:
    return len(label) * 11 * 0.62 + 14


def chip(x: float, y: float, label: str, fg: str, bg: str) -> str:
    w = chip_width(label)
    return (
        f'<g><rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="20" rx="10" fill="{bg}" '
        f'stroke="{fg}" stroke-opacity=".35"/>'
        f'<text class="m" x="{num(x + w / 2)}" y="{num(y + 14)}" text-anchor="middle" '
        f'style="font-size:11px;fill:{fg}">{esc(label)}</text></g>'
    )


@dataclass(frozen=True)
class Stage:
    n: int
    title: str
    desc: str
    x: float
    y: float
    w: float
    h: float
    start: float
    end: float


def render_how_it_works(theme: str, forged: CliRun) -> str:
    c = THEMES[theme]
    width, height = 900.0, 494.0
    cycle = 14.5
    tl = Timeline(cycle)
    out: list[str] = []

    row1_y, row1_h = 122.0, 118.0
    row2_y, row2_h = 290.0, 178.0
    xs = (28.0, 244.0, 460.0, 676.0)
    stages = [
        Stage(1, "Ingest", "trace · HAR · screenshots", xs[0], row1_y, 196, row1_h, 1.3, 2.5),
        Stage(2, "Redact", "secrets & PII → HMAC tokens", xs[1], row1_y, 196, row1_h, 2.5, 3.7),
        Stage(
            3, "Extract claims", "atomic, quoted, key-flagged", xs[2], row1_y, 196, row1_h, 3.7, 5.1
        ),
        Stage(4, "Predicates", "what would prove each claim", xs[3], row1_y, 196, row1_h, 5.1, 6.5),
        Stage(
            5, "Checkers", "deterministic first · vision last", 28, row2_y, 520, row2_h, 6.5, 8.9
        ),
        Stage(6, "Verdict", "evidence-cited, exit code for CI", 572, row2_y, 300, row2_h, 8.9, 9.6),
    ]

    out.append(
        f'<rect x=".5" y=".5" width="{num(width - 1)}" height="{num(height - 1)}" rx="14" '
        f'fill="{c["panel"]}" stroke="{c["panel_line"]}"/>'
    )
    out.append(
        '<text x="28" y="42" class="b" style="font-size:19px">How a verdict is reached</text>'
        '<text x="28" y="66" class="t2" style="font-size:13px">Claims come from the agent. '
        "Proof comes from the recording.</text>"
    )

    bubble = tl.appear(0.2, fade=0.4)
    out.append(
        f'<g class="{bubble}">'
        f'<rect x="530" y="22" width="342" height="58" rx="12" fill="{c["card"]}" '
        f'stroke="{c["border"]}"/>'
        f'<path d="M690 79 l10 12 l8 -12 z" fill="{c["card"]}" stroke="{c["border"]}"/>'
        f'<rect x="686" y="76" width="26" height="5" fill="{c["card"]}"/>'
        f'<text x="548" y="42" class="mu sb" style="font-size:10px;letter-spacing:1.2px">'
        "AGENT SAYS</text>"
        f'<text x="548" y="65" class="i" style="font-size:14.5px">'
        "“I placed the order, order #1042 confirmed.”</text></g>"
    )

    arrow = (
        f'<marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{c["muted"]}"/></marker>'
    )
    out.append(
        f"<defs>{arrow}"
        f'<filter id="glow" x="-10%" y="-10%" width="120%" height="130%">'
        f'<feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="{c["accent"]}" '
        f'flood-opacity=".55"/></filter>'
        f'<filter id="soft" x="-10%" y="-10%" width="120%" height="130%">'
        f'<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="{c["shadow"]}" '
        f'flood-opacity=".08"/></filter></defs>'
    )
    line = f'stroke="{c["muted"]}" stroke-width="1.6" fill="none" marker-end="url(#ah)"'
    for a, b in itertools.pairwise(xs):
        y = row1_y + row1_h / 2
        out.append(f'<path d="M{num(a + 198)} {num(y)}H{num(b - 3)}" {line}/>')
    out.append(
        f'<path d="M774 {num(row1_y + row1_h + 2)}C774 270 420 262 420 {num(row2_y - 3)}" {line}/>'
    )
    out.append(f'<path d="M550 {num(row2_y + 80)}H{num(569)}" {line}/>')

    for st in stages:
        out.append(
            f'<rect x="{num(st.x)}" y="{num(st.y)}" width="{num(st.w)}" height="{num(st.h)}" rx="12" '
            f'fill="{c["card"]}" stroke="{c["border"]}" filter="url(#soft)"/>'
        )
        stroke = c["red"] if st.n == 6 else c["accent"]
        settle = "1" if st.n == 6 else ".45"
        lit = tl.keyframes(
            [
                (0, "opacity:0"),
                (st.start, "opacity:0"),
                (st.start + 0.2, "opacity:1"),
                (st.end, "opacity:1"),
                (st.end + 0.3, f"opacity:{settle}"),
                (cycle - 0.5, f"opacity:{settle}"),
                (cycle - 0.1, "opacity:0"),
                (cycle, "opacity:0"),
            ]
        )
        out.append(
            f'<rect class="{lit}" x="{num(st.x)}" y="{num(st.y)}" width="{num(st.w)}" '
            f'height="{num(st.h)}" rx="12" fill="none" stroke="{stroke}" stroke-width="2" '
            f'filter="url(#glow)" style="opacity:{settle}"/>'
        )
        badge_fill = c["red"] if st.n == 6 else c["accent"]
        out.append(
            f'<circle cx="{num(st.x + 22)}" cy="{num(st.y + 24)}" r="11" fill="{badge_fill}"/>'
            f'<text x="{num(st.x + 22)}" y="{num(st.y + 28.5)}" text-anchor="middle" class="b" '
            f'style="font-size:12px;fill:#fff">{st.n}</text>'
            f'<text x="{num(st.x + 41)}" y="{num(st.y + 29)}" class="b" style="font-size:15px">'
            f"{esc(st.title)}</text>"
            f'<text x="{num(st.x + 16)}" y="{num(st.y + 55)}" class="t2" style="font-size:11.5px">'
            f"{esc(st.desc)}</text>"
        )

    def reveal(t: float, inner: str) -> str:
        return f'<g class="{tl.appear(t, fade=0.25)}">{inner}</g>'

    s1, s2, s3, s4, s5, s6 = stages
    ingest_chips = [f"net#1{DASH}5", f"snap#1{DASH}7", "final"]
    cx = s1.x + 16
    chip_pos: dict[str, tuple[float, float]] = {}
    for idx, label in enumerate(ingest_chips):
        row = 0 if idx < 2 else 1
        if idx == 2:
            cx = s1.x + 16
        out.append(
            reveal(
                s1.start + 0.15 + idx * 0.15,
                chip(cx, s1.y + 66 + row * 26, label, c["chip"], c["chip_soft"]),
            )
        )
        chip_pos[label] = (cx, s1.y + 66 + row * 26)
        cx += chip_width(label) + 6
    out.append(
        reveal(
            s2.start + 0.2,
            chip(s2.x + 16, s2.y + 66, "you@shop.test", c["muted"], c["track"])
            + f'<line x1="{num(s2.x + 20)}" x2="{num(s2.x + 14 + chip_width("you@shop.test") - 4)}" '
            f'y1="{num(s2.y + 76)}" y2="{num(s2.y + 76)}" stroke="{c["red"]}" stroke-width="1.4"/>',
        )
    )
    out.append(
        reveal(
            s2.start + 0.55,
            chip(s2.x + 16, s2.y + 92, "→ [email:3f9a]", c["green"], c["green_soft"]),
        )
    )
    out.append(
        reveal(
            s3.start + 0.25,
            chip(s3.x + 16, s3.y + 66, "placed an order · key", c["accent"], c["accent_soft"]),
        )
    )
    out.append(
        reveal(
            s3.start + 0.55,
            chip(s3.x + 16, s3.y + 92, "order #1042 shown · key", c["accent"], c["accent_soft"]),
        )
    )
    out.append(
        reveal(
            s4.start + 0.25, chip(s4.x + 16, s4.y + 66, "POST ~order → 2xx", c["text2"], c["track"])
        )
    )
    out.append(
        reveal(s4.start + 0.55, chip(s4.x + 16, s4.y + 92, "text ∋ '1042'", c["text2"], c["track"]))
    )

    checkers = ["request", "page text", "URL", "value", "screenshot"]
    px = s5.x + 16
    pill_y = s5.y + 68
    for name in checkers:
        w = 88.0 if name != "URL" else 66.0
        hot = name in ("request", "page text")
        out.append(
            f'<rect x="{num(px)}" y="{num(pill_y)}" width="{num(w)}" height="24" rx="7" '
            f'fill="{c["track"]}" stroke="{c["border"]}"/>'
            f'<text x="{num(px + w / 2)}" y="{num(pill_y + 16)}" text-anchor="middle" class="t2" '
            f'style="font-size:11.5px">{name}</text>'
        )
        if hot:
            on = tl.keyframes(
                [
                    (0, "opacity:0"),
                    (s5.start + 0.3, "opacity:0"),
                    (s5.start + 0.5, "opacity:1"),
                    (cycle - 0.5, "opacity:1"),
                    (cycle - 0.1, "opacity:0"),
                    (cycle, "opacity:0"),
                ]
            )
            out.append(
                f'<g class="{on}"><rect x="{num(px)}" y="{num(pill_y)}" width="{num(w)}" height="24" '
                f'rx="7" fill="{c["accent_soft"]}" stroke="{c["accent"]}" stroke-width="1.5"/>'
                f'<text x="{num(px + w / 2)}" y="{num(pill_y + 16)}" text-anchor="middle" class="sb" '
                f'style="font-size:11.5px;fill:{c["accent"]}">{name}</text></g>'
            )
        px += w + 8

    results = [
        ("✗", c["red"], "no POST /api/orders · closest was POST /api/cart", "net#3"),
        ("✓", c["green"], "'confirm' text is on the final page", "snap#6"),
        ("✗", c["red"], "'1042' in no page state and no response", "final"),
    ]
    land: dict[str, tuple[float, float]] = {}
    for idx, (mark, color, text, ev) in enumerate(results):
        y = s5.y + 116 + idx * 23
        t = s5.start + 1.15 + idx * 0.25
        land[ev] = (s5.x + s5.w - 16 - chip_width(ev), y - 14)
        out.append(
            reveal(
                t,
                f'<text x="{num(s5.x + 18)}" y="{num(y)}" class="b" style="font-size:13px;fill:{color}">'
                f"{mark}</text>"
                f'<text x="{num(s5.x + 36)}" y="{num(y)}" style="font-size:12px">{esc(text)}</text>',
            )
        )
    flights = [("net#3", ingest_chips[0]), ("snap#6", ingest_chips[1]), ("final", ingest_chips[2])]
    for idx, (ev, origin) in enumerate(flights):
        ox, oy = chip_pos[origin]
        lx, ly = land[ev]
        dx, dy = ox - lx, oy - ly
        t0 = s5.start + 0.25 + idx * 0.22
        t1 = t0 + 0.75
        fly = tl.keyframes(
            [
                (0, f"opacity:0;transform:translate({num(dx)}px,{num(dy)}px)"),
                (t0, f"opacity:0;transform:translate({num(dx)}px,{num(dy)}px)"),
                (
                    t0 + 0.08,
                    f"opacity:1;transform:translate({num(dx)}px,{num(dy)}px);animation-timing-function:cubic-bezier(.45,0,.2,1)",
                ),
                (t1, "opacity:1;transform:translate(0,0)"),
                (cycle - 0.5, "opacity:1;transform:translate(0,0)"),
                (cycle - 0.1, "opacity:0;transform:translate(0,0)"),
                (cycle, "opacity:0"),
            ]
        )
        out.append(f'<g class="{fly}">{chip(lx, ly, ev, c["chip"], c["chip_soft"])}</g>')

    token_label = "“order #1042 confirmed”"
    tw = len(token_label) * 6.4 + 20
    track_y1 = row1_y - 22
    track_y2 = (row1_y + row1_h + row2_y) / 2 - 2
    path = [
        (0.6, 700.0, 84.0),
        (1.25, s1.x + s1.w / 2, track_y1),
        (s2.start, s2.x + s2.w / 2, track_y1),
        (s3.start, s3.x + s3.w / 2, track_y1),
        (s4.start, s4.x + s4.w / 2, track_y1),
        (s5.start, s5.x + s5.w / 2 + 140, track_y2),
        (s6.start - 0.15, s6.x + s6.w / 2, track_y2),
    ]
    frames: list[tuple[float, str]] = [
        (0, f"opacity:0;transform:translate({num(path[0][1])}px,{num(path[0][2])}px)")
    ]
    for k, (t, x, y) in enumerate(path):
        opacity = "0" if k == 0 else "1"
        frames.append(
            (
                t,
                f"opacity:{opacity};transform:translate({num(x)}px,{num(y)}px);animation-timing-function:cubic-bezier(.5,0,.25,1)",
            )
        )
        if 0 < k < len(path) - 1:
            hold_t = min(t + 0.55, path[k + 1][0] - 0.05)
            frames.append(
                (
                    hold_t,
                    f"opacity:1;transform:translate({num(x)}px,{num(y)}px);animation-timing-function:cubic-bezier(.5,0,.25,1)",
                )
            )
    last_x, last_y = path[-1][1], path[-1][2]
    frames.append(
        (s6.start + 0.2, f"opacity:0;transform:translate({num(last_x)}px,{num(last_y + 30)}px)")
    )
    frames.append((cycle, f"opacity:0;transform:translate({num(last_x)}px,{num(last_y + 30)}px)"))
    token = tl.keyframes(frames)
    out.append(
        f'<g class="{token}" style="opacity:0">'
        f'<rect x="{num(-tw / 2)}" y="-11" width="{num(tw)}" height="22" rx="11" fill="{c["accent"]}"/>'
        f'<text x="0" y="4" text-anchor="middle" class="i sb" style="font-size:11.5px;fill:#fff">'
        f"{esc(token_label)}</text></g>"
    )

    stamp_t = s6.start + 0.1
    sx, sy = s6.x + s6.w / 2, s6.y + 104
    slam = tl.keyframes(
        [
            (0, "opacity:0;transform:scale(2.4)"),
            (
                stamp_t,
                "opacity:0;transform:scale(2.4);animation-timing-function:cubic-bezier(.6,0,.9,.4)",
            ),
            (stamp_t + 0.22, "opacity:1;transform:scale(.94)"),
            (stamp_t + 0.36, "opacity:1;transform:scale(1.03)"),
            (stamp_t + 0.48, "opacity:1;transform:scale(1)"),
            (cycle - 0.5, "opacity:1;transform:scale(1)"),
            (cycle - 0.1, "opacity:0;transform:scale(1)"),
            (cycle, "opacity:0"),
        ]
    )
    shake = tl.keyframes(
        [
            (0, "transform:none"),
            (stamp_t + 0.22, "transform:none"),
            (stamp_t + 0.27, "transform:translate(-3px,2px)"),
            (stamp_t + 0.32, "transform:translate(3px,-1px)"),
            (stamp_t + 0.37, "transform:translate(-1px,1px)"),
            (stamp_t + 0.42, "transform:none"),
            (cycle, "transform:none"),
        ]
    )
    out.append(
        f'<g class="{shake}"><g transform="translate({num(sx)} {num(sy)}) rotate(-7)">'
        f'<g class="{slam}" style="transform-box:view-box;transform-origin:0 0">'
        f'<rect x="-74" y="-26" width="148" height="52" rx="8" fill="{c["red_soft"]}" '
        f'stroke="{c["red"]}" stroke-width="3"/>'
        f'<rect x="-68" y="-20" width="136" height="40" rx="5" fill="none" stroke="{c["red"]}" '
        f'stroke-width="1.2" stroke-opacity=".6"/>'
        f'<text x="0" y="11" text-anchor="middle" class="b" style="font-size:31px;'
        f'letter-spacing:6px;fill:{c["red"]}">FAIL</text></g></g></g>'
    )
    out.append(
        reveal(
            stamp_t + 0.6,
            f'<text x="{num(sx)}" y="{num(s6.y + 154)}" text-anchor="middle" class="sb" '
            f'style="font-size:12.5px">no <tspan class="m" style="fill:{c["red"]}">POST /api/orders</tspan>'
            " was ever sent</text>"
            f'<text x="{num(sx)}" y="{num(s6.y + 170)}" text-anchor="middle" class="m mu" '
            f'style="font-size:11px">{esc(score_of(forged))} · exit {forged.exit_code}</text>',
        )
    )

    title = (
        "How Evidence Judge works: ingest, redact, extract claims, predicates, checkers, verdict"
    )
    return svg_doc(width, height, title, base_style(c) + tl.css() + REDUCED_MOTION, "".join(out))


@dataclass(frozen=True)
class BenchRow:
    judge: str
    kind: str
    n: int
    false_pass: int
    review: int
    fail: int
    errors: int


def load_bench(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def bench_rows(data: dict[str, Any]) -> list[BenchRow]:
    rows: list[BenchRow] = []
    for m in data["forgery_metrics"]:
        rows.append(
            BenchRow(
                judge=m["judge"],
                kind=m["kind"],
                n=m["false_pass"]["n"],
                false_pass=m["false_pass"]["k"],
                review=m["needs_review"],
                fail=m["fail"],
                errors=m["errors"],
            )
        )
    return rows


JUDGE_LABELS = {
    "evidence-judge+spec": "Evidence Judge + spec",
    "evidence-judge": "Evidence Judge, no spec",
}


def render_benchmark(theme: str, data: dict[str, Any]) -> str:
    c = THEMES[theme]
    rows = bench_rows(data)
    by_key = {(r.judge, r.kind): r for r in rows}
    judges: list[str] = list(data["judges"])
    kinds: list[str] = list(data["kinds"])
    pooled = [r for r in rows if r.kind == "all manipulations"]
    if not pooled:
        raise SystemExit("bench results have no 'all manipulations' row")
    worst = max(pooled, key=lambda r: r.false_pass / r.n if r.n else 0.0)
    pooled_metric = next(
        m
        for m in data["forgery_metrics"]
        if m["judge"] == worst.judge and m["kind"] == "all manipulations"
    )
    ci_high = float(pooled_metric["false_pass"]["ci_high"])
    forged_scenarios = sum(1 for s in data["scenarios"] if s["forged"])
    seeds = int(data["seeds"])

    same = all(r.false_pass == worst.false_pass and r.n == worst.n for r in pooled)
    scope = (
        "with and without a spec"
        if same
        else f"worst: {JUDGE_LABELS.get(worst.judge, worst.judge)}"
    )

    width = 900.0
    track_x, track_w = 262.0, 452.0
    kind_h = 22 + 18 * (len(judges) + 1) + 14
    top = 152.0
    height = top + kind_h * len(kinds) + 46
    out: list[str] = [
        f'<rect x=".5" y=".5" width="{num(width - 1)}" height="{num(height - 1)}" rx="14" '
        f'fill="{c["panel"]}" stroke="{c["panel_line"]}"/>',
        '<text x="28" y="42" class="b" style="font-size:19px">Can a forged success story fool the judge?</text>',
        '<text x="28" y="65" class="t2" style="font-size:12.5px">Failed runs rewritten to claim '
        "success, evidence untouched: PASS is always wrong.</text>",
        f'<text x="28" y="84" class="mu" style="font-size:12px">{forged_scenarios} failed runs {TIMES} '
        f"{len(kinds)} forgery kinds {TIMES} {seeds} seeds per judge, judged offline · "
        "bench/results/offline-fixtures.json</text>",
    ]
    hero = (
        f'<g class="hero"><rect x="626" y="20" width="246" height="72" rx="12" fill="{c["card"]}" '
        f'stroke="{c["border"]}"/>'
        f'<text x="644" y="56" class="b" style="font-size:30px;fill:{c["green"]}">'
        f"{worst.false_pass} of {worst.n}</text>"
        f'<text x="{num(644 + len(f"{worst.false_pass} of {worst.n}") * 17.5 + 8)}" y="45" '
        f'class="sb" style="font-size:12px">forged success</text>'
        f'<text x="{num(644 + len(f"{worst.false_pass} of {worst.n}") * 17.5 + 8)}" y="60" '
        f'class="sb" style="font-size:12px">stories passed</text>'
        f'<text x="644" y="80" class="mu" style="font-size:11px">95% CI 0{DASH}{round(ci_high * 100)}% · '
        f"{esc(scope)}</text></g>"
    )
    out.append(hero)

    legend_y = 120.0
    items = [
        ("FAIL (caught)", c["fail_bar"], False),
        ("NEEDS_REVIEW (escalated)", c["review_bar"], False),
        ("PASS (fooled)", c["pass_bar"], False),
        ("typical LLM judge: not yet measured", c["muted"], True),
    ]
    lx = 28.0
    for label, color, dashed in items:
        if dashed:
            sw = (
                f'<rect x="{num(lx)}" y="{num(legend_y - 9)}" width="22" height="11" rx="3" '
                f'fill="none" stroke="{color}" stroke-dasharray="3 2"/>'
            )
        else:
            sw = f'<rect x="{num(lx)}" y="{num(legend_y - 9)}" width="22" height="11" rx="3" fill="{color}"/>'
        out.append(
            sw
            + f'<text x="{num(lx + 29)}" y="{num(legend_y)}" class="t2" style="font-size:12px">{esc(label)}</text>'
        )
        lx += 29 + len(label) * 6.3 + 26

    axis_y = top + kind_h * len(kinds) + 4
    for frac in (0.0, 0.5, 1.0):
        x = track_x + track_w * frac
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        out.append(
            f'<line x1="{num(x)}" x2="{num(x)}" y1="{num(top + 18)}" y2="{num(axis_y)}" '
            f'stroke="{c["panel_line"]}" stroke-dasharray="2 3"/>'
            f'<text x="{num(x)}" y="{num(axis_y + 15)}" text-anchor="{anchor}" class="mu" '
            f'style="font-size:11px">{round(frac * 100)}%</text>'
        )
    out.append(
        f'<text x="{num(track_x + track_w / 2)}" y="{num(axis_y + 31)}" text-anchor="middle" class="mu" '
        'style="font-size:11px">share of forged runs, by verdict</text>'
    )
    css_rules: list[str] = []
    seq = 0
    for ki, kind in enumerate(kinds):
        y0 = top + ki * kind_h
        tag = "  (control: restyled, no new success claim)" if kind == "style_only" else ""
        out.append(
            f'<text x="28" y="{num(y0 + 14)}" class="m b" style="font-size:12.5px">{esc(kind)}'
            f'<tspan class="mu" style="font-weight:400;font-family:{SANS};font-size:11.5px">{esc(tag)}</tspan></text>'
        )
        for ji, judge in enumerate([*judges, "llm"]):
            ry = y0 + 24 + ji * 18
            label = JUDGE_LABELS.get(judge, judge) if judge != "llm" else "Typical LLM judge"
            out.append(
                f'<text x="44" y="{num(ry + 10)}" class="t2" style="font-size:12px">{esc(label)}</text>'
            )
            if judge == "llm":
                out.append(
                    f'<rect x="{num(track_x)}" y="{num(ry)}" width="{num(track_w)}" height="12" rx="3" '
                    f'fill="{c["panel"]}" stroke="{c["muted"]}" stroke-dasharray="4 3"/>'
                    f'<text x="{num(track_x + track_w / 2)}" y="{num(ry + 9.5)}" text-anchor="middle" '
                    f'class="mu" style="font-size:10px">not yet measured · run --with-llm-baseline</text>'
                    f'<text x="{num(track_x + track_w + 14)}" y="{num(ry + 10)}" class="mu" '
                    f'style="font-size:12px">n/a</text>'
                )
                continue
            row = by_key[(judge, kind)]
            out.append(
                f'<rect x="{num(track_x)}" y="{num(ry)}" width="{num(track_w)}" height="12" rx="3" fill="{c["track"]}"/>'
            )
            segs = [
                (row.fail, c["fail_bar"]),
                (row.review, c["review_bar"]),
                (row.false_pass, c["pass_bar"]),
            ]
            sx = track_x
            bars: list[str] = []
            counts: list[str] = []
            for count, color in segs:
                if not count or not row.n:
                    continue
                w = track_w * count / row.n
                gap = 2.0 if sx + w < track_x + track_w - 0.5 else 0.0
                bars.append(
                    f'<rect x="{num(sx)}" y="{num(ry)}" width="{num(max(w - gap, 1))}" height="12" rx="3" fill="{color}"/>'
                )
                if w >= 26:
                    counts.append(
                        f'<text x="{num(sx + w / 2 - gap / 2)}" y="{num(ry + 9.5)}" text-anchor="middle" '
                        f'class="b" style="font-size:9.5px;fill:#fff">{count}</text>'
                    )
                sx += w
            name = f"g{seq}"
            delay = 0.25 + seq * 0.09
            seq += 1
            css_rules.append(
                f".{name}{{transform-box:view-box;transform-origin:{num(track_x)}px 0;"
                f"animation:grow .9s cubic-bezier(.2,.8,.2,1) {num(delay)}s both}}"
            )
            out.append(f'<g class="{name}">{"".join(bars)}</g>')
            out.append(f'<g class="f{name}">{"".join(counts)}</g>')
            rate = round(100 * row.false_pass / row.n) if row.n else 0
            css_rules.append(f".f{name}{{animation:fade .5s ease-out {num(delay + 0.6)}s both}}")
            out.append(
                f'<text class="f{name}" x="{num(track_x + track_w + 14)}" y="{num(ry + 10)}" '
                f'style="font-size:12px"><tspan class="b">{rate}%</tspan>'
                f'<tspan class="t2"> false PASS · {row.false_pass}/{row.n}</tspan></text>'
            )
    style = (
        base_style(c)
        + "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        + "@keyframes fade{from{opacity:0}to{opacity:1}}"
        + ".hero{animation:fade .6s ease-out .1s both}"
        + "".join(css_rules)
        + REDUCED_MOTION
    )
    title = f"Forged-run benchmark: {worst.false_pass} of {worst.n} forged success stories passed"
    return svg_doc(width, height + 10, title, style, "".join(out))


def render_mascot() -> str:
    brown, brown_dark, face, wig, wig_line = "#8a5a3b", "#6b4329", "#efd9b8", "#f6f2e9", "#c9bfa9"
    curls_left = "".join(
        f'<circle cx="{num(41 + (k % 2) * 3)}" cy="{num(54 + k * 9)}" r="7.5" fill="{wig}" stroke="{wig_line}" stroke-width="1.2"/>'
        for k in range(6)
    )
    curls_right = "".join(
        f'<circle cx="{num(119 - (k % 2) * 3)}" cy="{num(54 + k * 9)}" r="7.5" fill="{wig}" stroke="{wig_line}" stroke-width="1.2"/>'
        for k in range(6)
    )

    def eye(cx: float) -> str:
        return (
            f'<circle cx="{num(cx)}" cy="70" r="13" fill="#fff" stroke="{brown_dark}" stroke-width="1.2"/>'
            f'<circle cx="{num(cx)}" cy="71" r="8.5" fill="#f2a900"/>'
            f'<circle cx="{num(cx)}" cy="71" r="4.6" fill="#1d1a2b"/>'
            f'<circle cx="{num(cx + 2.6)}" cy="68" r="1.8" fill="#fff"/>'
            f'<ellipse class="lid" cx="{num(cx)}" cy="70" rx="13.6" ry="13.6" fill="{brown}"/>'
        )

    body = (
        '<defs><radialGradient id="bg" cx="50%" cy="45%" r="55%">'
        '<stop offset="0" stop-color="#c7d2fe" stop-opacity=".75"/>'
        '<stop offset="1" stop-color="#a5b4fc" stop-opacity=".18"/></radialGradient>'
        '<clipPath id="lens"><circle cx="134" cy="104" r="15"/></clipPath></defs>'
        '<circle cx="80" cy="84" r="74" fill="url(#bg)"/>'
        f'<path d="M40 108Q80 92 120 108L132 150Q80 160 28 150Z" fill="#2f2b4a" stroke="#1d1a2b" stroke-width="1.5"/>'
        '<path d="M80 104L66 150M80 104L94 150" stroke="#463f6b" stroke-width="2" fill="none"/>'
        '<rect x="73" y="102" width="6" height="20" rx="1.5" fill="#fff" stroke="#d4d4d8"/>'
        '<rect x="81" y="102" width="6" height="20" rx="1.5" fill="#fff" stroke="#d4d4d8"/>'
        '<g class="head">'
        f'<path d="M47 44L40 20L62 36Z" fill="{brown_dark}"/><path d="M113 44L120 20L98 36Z" fill="{brown_dark}"/>'
        f'<ellipse cx="80" cy="70" rx="41" ry="37" fill="{brown}" stroke="{brown_dark}" stroke-width="1.5"/>'
        f'<path d="M45 60Q44 30 80 29Q116 30 115 60Q100 47 80 47Q60 47 45 60Z" fill="{wig}" stroke="{wig_line}" stroke-width="1.2"/>'
        f'<path d="M56 41Q80 31 104 41M51 50Q80 37 109 50" fill="none" stroke="{wig_line}" stroke-width="1.1"/>'
        f"{curls_left}{curls_right}"
        f'<circle cx="64" cy="71" r="18" fill="{face}"/><circle cx="96" cy="71" r="18" fill="{face}"/>'
        f"{eye(64)}{eye(96)}"
        '<path d="M80 79L74 87L80 96L86 87Z" fill="#e8891c" stroke="#b9650c" stroke-width="1"/>'
        "</g>"
        '<line x1="112" y1="134" x2="124" y2="117" stroke="#7c4a1e" stroke-width="6" stroke-linecap="round"/>'
        '<circle cx="134" cy="104" r="15" fill="#8ec5f2" fill-opacity=".85"/>'
        '<g clip-path="url(#lens)"><rect class="glint" x="125" y="80" width="8" height="50" fill="#fff" '
        'opacity=".85" transform="rotate(25 134 104)"/></g>'
        '<circle cx="134" cy="104" r="15" fill="none" stroke="#5b6475" stroke-width="4"/><circle cx="134" cy="104" r="17.2" fill="none" stroke="#2b303b" stroke-width=".8"/>'
        f'<ellipse cx="114" cy="128" rx="9" ry="13" fill="{brown}" stroke="{brown_dark}" stroke-width="1.2" '
        'transform="rotate(-35 114 128)"/>'
    )
    style = (
        ".head{transform-box:view-box;transform-origin:80px 104px;"
        "animation:tilt 7s ease-in-out infinite}"
        "@keyframes tilt{0%,100%{transform:rotate(-2.5deg)}50%{transform:rotate(3deg)}}"
        ".lid{transform-box:fill-box;transform-origin:center;transform:scaleY(0);"
        "animation:blink 4.6s infinite}"
        "@keyframes blink{0%,90%,100%{transform:scaleY(0)}93%,95%{transform:scaleY(1)}}"
        ".glint{animation:glint 3.8s ease-in-out infinite}"
        "@keyframes glint{0%,55%{transform:translate(-30px,0) rotate(25deg)}"
        "80%,100%{transform:translate(30px,0) rotate(25deg)}}"
        ".glint{transform-box:view-box;transform-origin:134px 104px}" + REDUCED_MOTION
    )
    return svg_doc(160, 160, "Judge Owl, the Evidence Judge mascot", style, body)


def build() -> dict[str, str]:
    forged = run_judge("forged_success")
    honest = run_judge("honest_success")
    bench = load_bench(BENCH_RESULTS)
    assets: dict[str, str] = {
        "demo-forged.svg": render_terminal(
            forged, "evidence-judge judge on a forged success run: FAIL, exit code 1", hold=4.5
        ),
        "demo-honest.svg": render_terminal(
            honest, "evidence-judge judge on an honest success run: PASS, exit code 0", hold=4.0
        ),
        "mascot.svg": render_mascot(),
    }
    for theme in THEMES:
        assets[f"how-it-works-{theme}.svg"] = render_how_it_works(theme, forged)
        assets[f"benchmark-{theme}.svg"] = render_benchmark(theme, bench)
    return assets


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
    if args.check:
        for name in stale:
            print(f"out of date: docs/assets/{name}")
        return 1 if stale else 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, svg in assets.items():
        (ASSETS / name).write_text(svg, encoding="utf-8")
        print(f"wrote docs/assets/{name} ({len(svg.encode()) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
