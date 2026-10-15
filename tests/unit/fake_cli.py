"""A fake coding-agent CLI for testing the CLI LLM backends without the real binaries.

`FakeCLI.install(name)` writes an executable called `name` into a temporary directory and puts
that directory first on PATH. Each run of it records its argv, stdin, working directory, the
files in that directory and a few environment variables, then plays back the next queued reply
(stdout, stderr, exit code, optional delay). The last reply repeats once the queue is used up.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = """\
#!{python}
import json, os, sys, time
from pathlib import Path

state = Path({state!r})
log = state / "calls.jsonl"
count = len(log.read_text().splitlines()) if log.exists() else 0
stdin = sys.stdin.read()
files = {{}}
for path in sorted(Path.cwd().rglob("*")):
    if path.is_file():
        data = path.read_bytes()
        try:
            files[str(path.relative_to(Path.cwd()))] = data.decode("utf-8")
        except UnicodeDecodeError:
            files[str(path.relative_to(Path.cwd()))] = "<%d bytes>" % len(data)
record = {{
    "argv": sys.argv[1:],
    "stdin": stdin,
    "cwd": os.getcwd(),
    "files": files,
    "env": {{k: os.environ.get(k) for k in ("GEMINI_SYSTEM_MD", "NO_COLOR")}},
}}
with log.open("a") as handle:
    handle.write(json.dumps(record) + "\\n")
replies = json.loads((state / "replies.json").read_text())
reply = replies[min(count, len(replies) - 1)]
time.sleep(reply.get("sleep", 0))
sys.stdout.write(reply.get("stdout", ""))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
"""


@dataclass
class Call:
    argv: list[str]
    stdin: str
    cwd: str
    files: dict[str, str]
    env: dict[str, str | None]

    def flag(self, name: str) -> str:
        """The value after `name` in argv (the last one if repeated)."""
        indexes = [i for i, arg in enumerate(self.argv) if arg == name]
        assert indexes, f"{name} not in {self.argv}"
        return self.argv[indexes[-1] + 1]

    def values(self, name: str) -> list[str]:
        return [self.argv[i + 1] for i, arg in enumerate(self.argv) if arg == name]


class FakeCLI:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = root / "bin"
        self.state = root / "state"
        self.bin.mkdir(parents=True)
        self.state.mkdir(parents=True)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        self.replies()

    def install(self, name: str) -> Path:
        path = self.bin / name
        path.write_text(_SCRIPT.format(python=sys.executable, state=str(self.state)))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def replies(self, *replies: dict[str, Any]) -> None:
        payload = list(replies) or [{"stdout": "", "exit": 0}]
        (self.state / "replies.json").write_text(json.dumps(payload))

    @property
    def calls(self) -> list[Call]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [Call(**json.loads(line)) for line in log.read_text().splitlines()]


def jsonl(*events: dict[str, Any]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)
