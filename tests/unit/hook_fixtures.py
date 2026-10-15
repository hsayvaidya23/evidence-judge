"""Builders for agent-hook tests: synthetic hook payloads and a project with a recorded trace."""

from __future__ import annotations

import json
import shutil
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


def raw_trace(run: str, traces_dir: Path, stem: str = "trace-1792007306000") -> Path:
    """Unzip a fixture trace into the layout Playwright MCP writes: ``<stem>.trace``,
    ``<stem>.network`` and every blob in a shared ``resources/`` directory."""
    resources = traces_dir / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(RUNS / run / "trace.zip") as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name == "trace.trace":
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
        trace = root / "test-results" / "shop-buy-chromium" / "trace.zip"
        trace.parent.mkdir(parents=True)
        shutil.copyfile(RUNS / run / "trace.zip", trace)
    if spec:
        (root / ".evidence-judge").mkdir()
        shutil.copyfile(RUNS / run / "spec.yaml", root / ".evidence-judge" / "spec.yaml")
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
