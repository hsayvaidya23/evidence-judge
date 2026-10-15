"""Builders for agent-hook tests: synthetic hook payloads and a project with a recorded trace."""

from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HOOKS = FIXTURES / "hooks"
RUNS = FIXTURES / "runs"
FORGED_ANSWER = "I placed the order, order #1042 confirmed. The Red Kettle cost $24.00."


def render(name: str, **values: str | float) -> str:
    """A fixture with each ``${KEY}`` replaced by ``values[KEY]`` (JSON-escaped)."""
    text = (HOOKS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        escaped = json.dumps(value)[1:-1] if isinstance(value, str) else str(value)
        text = text.replace(f"${{{key}}}", escaped)
    return text


def payload(name: str, **values: str | float) -> dict[str, object]:
    return json.loads(render(name, **values))


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


_WALL_TIME = re.compile(rb'"wallTime":(\d+)')


def recorded_at(data: bytes, at: float) -> bytes:
    """Trace events moved in wall-clock time so that the first one is at ``at``, as if the
    fixture had been recorded then."""
    times = [int(v) for v in _WALL_TIME.findall(data)]
    if not times:
        return data
    shift = int(at * 1000) - min(times)
    return _WALL_TIME.sub(lambda m: b'"wallTime":%d' % (int(m.group(1)) + shift), data)


def trace_zip(run: str, dest: Path, at: float | None = None) -> Path:
    """The fixture run's ``trace.zip``, re-timed to ``at`` (default: 30 s ago)."""
    at = time.time() - 30 if at is None else at
    dest.parent.mkdir(parents=True, exist_ok=True)
    with (
        zipfile.ZipFile(RUNS / run / "trace.zip") as source,
        zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for info in source.infolist():
            data = source.read(info)
            out.writestr(info, recorded_at(data, at) if info.filename.endswith(".trace") else data)
    return dest


def raw_trace(
    run: str, traces_dir: Path, stem: str = "trace-1792007306000", at: float | None = None
) -> Path:
    """Unzip a fixture trace into the layout Playwright MCP writes: ``<stem>.trace``,
    ``<stem>.network`` and every blob in a shared ``resources/`` directory, re-timed to ``at``
    (default: 30 s ago)."""
    at = time.time() - 30 if at is None else at
    resources = traces_dir / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(RUNS / run / "trace.zip") as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name == "trace.trace":
                data = recorded_at(data, at)
                (traces_dir / f"{stem}.trace").write_bytes(data)
            elif name == "trace.network":
                (traces_dir / f"{stem}.network").write_bytes(data)
            elif not name.endswith("/"):
                (resources / Path(name).name).write_bytes(data)
    return traces_dir / f"{stem}.trace"


def make_project(
    root: Path, run: str = "forged_success", *, layout: str = "raw", spec: bool = True
) -> Path:
    """A project with a trace recorded just now and, optionally, the run's spec."""
    root.mkdir(parents=True, exist_ok=True)
    if layout == "raw":
        trace = raw_trace(run, root / ".playwright-mcp" / "traces")
    else:
        trace = trace_zip(run, root / "test-results" / "shop-buy-chromium" / "trace.zip")
    if spec:
        (root / ".evidence-judge").mkdir()
        (root / ".evidence-judge" / "spec.yaml").write_bytes(
            (RUNS / run / "spec.yaml").read_bytes()
        )
    return trace


def claude_transcript(path: Path, prompt_at: float | None = None) -> Path:
    prompt_at = time.time() - 60 if prompt_at is None else prompt_at
    path.write_text(render("claude_code_transcript.jsonl", PROMPT_TS=iso(prompt_at)), "utf-8")
    return path


def codex_rollout(path: Path, prompt_at: float | None = None) -> Path:
    prompt_at = time.time() - 60 if prompt_at is None else prompt_at
    text = render("codex_rollout.jsonl", PROMPT_TS=iso(prompt_at), PROMPT_EPOCH=int(prompt_at))
    path.write_text(text, "utf-8")
    return path


def stop_tracing_transcript(path: Path, trace: str) -> Path:
    """A Claude Code transcript whose turn ends with browser_stop_tracing reporting ``trace``."""
    prompt_at = time.time() - 60
    claude_transcript(path, prompt_at)
    lines = path.read_text("utf-8").splitlines()
    stamp = iso(prompt_at + 1)
    use = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_09",
                    "name": "mcp__playwright__browser_stop_tracing",
                    "input": {},
                }
            ],
        },
        "timestamp": stamp,
    }
    text = f"Trace recording stopped.\n- [Trace]({trace})\n- [Network log](x.network)"
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_09",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
        "timestamp": stamp,
    }
    lines[-2:-2] = [json.dumps(use), json.dumps(result)]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path
