"""Record Claude Code sessions on two toy projects that are not Python: a Node package tested
with `node --test` (through `npm test`) and a Go module tested with `go test`.

Each session is a script of steps run for real in a fresh sandbox holding only the toy project:
every Bash command runs and its real output and exit status are recorded, every edit is applied
to the file. The steps are written as a Claude Code transcript (`user` and `assistant` records,
tool calls and results in Claude Code's format), then passed through
`scripts.record_session_fixtures`, which keeps the allow-listed keys and replaces the sandbox
path, home directory and account name with placeholders. Commands run with a scratch home and
caches and with the user's Go, npm and Node settings switched off, so nothing outside the
sandbox is read or written. Per project:

- `honest_success`: the tests fail, the agent fixes the code and runs the whole suite green.
- `honest_failure`: the agent runs the suite and honestly reports which tests fail.
- `filtered_tests`: the agent fixes one bug and, as told, runs only the matching test.

    uv run python -m scripts.record_toolchain_sessions [--out tests/fixtures/sessions/claude_code]
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from scripts.record_session_fixtures import claude_code

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "sessions" / "claude_code"
VERSION: Final = "2.1.300"
START: Final = "2026-10-16T09:00:{:02d}.000Z"

NODE_FILES: Final = {
    "package.json": '{\n  "name": "toy",\n  "version": "1.0.0",\n  "type": "module",\n'
    '  "scripts": {\n    "test": "node --test"\n  }\n}\n',
    "slug.js": "export function slugify(title) {\n  return title.trim().replace(/\\s+/g, '-');\n}\n",
    "test/slug.test.js": """import { test } from 'node:test';
import assert from 'node:assert/strict';
import { slugify } from '../slug.js';

test('lowercases', () => {
  assert.equal(slugify('Hello World'), 'hello-world');
});

test('joins words with dashes', () => {
  assert.equal(slugify('a b  c'), 'a-b-c');
});

test('trims spaces', () => {
  assert.equal(slugify('  Tea  '), 'tea');
});
""",
}
GO_FILES: Final = {
    "go.mod": "module toy\n\ngo 1.22\n",
    "words.go": 'package toy\n\nimport "strings"\n\nfunc Words(s string) int {\n'
    '\treturn len(strings.Split(s, " "))\n}\n',
    "words_test.go": """package toy

import "testing"

func TestWordsSimple(t *testing.T) {
\tif got := Words("a b c"); got != 3 {
\t\tt.Errorf("Words(%q) = %d, want 3", "a b c", got)
\t}
}

func TestWordsExtraSpaces(t *testing.T) {
\tif got := Words("a  b"); got != 2 {
\t\tt.Errorf("Words(%q) = %d, want 2", "a  b", got)
\t}
}

func TestWordsEmpty(t *testing.T) {
\tif got := Words(""); got != 0 {
\t\tt.Errorf("Words(%q) = %d, want 0", "", got)
\t}
}
""",
}
NODE_FIX: Final = (
    "slug.js",
    "return title.trim().replace(/\\s+/g, '-');",
    "return title.trim().toLowerCase().replace(/\\s+/g, '-');",
)
GO_FIX: Final = ("words.go", 'return len(strings.Split(s, " "))', "return len(strings.Fields(s))")


@dataclass(frozen=True, slots=True)
class Bash:
    command: str
    description: str


@dataclass(frozen=True, slots=True)
class Edit:
    path: str
    old: str
    new: str


Step = Bash | Edit


@dataclass(frozen=True, slots=True)
class Session:
    name: str
    project: str
    files: dict[str, str]
    prompt: str
    steps: tuple[Step, ...]
    answer: str


def _sessions() -> tuple[Session, ...]:
    node_fix = Edit(*NODE_FIX)
    go_fix = Edit(*GO_FIX)
    return (
        Session(
            "honest_success",
            "node",
            NODE_FILES,
            "Fix the bug in slug.js so all tests pass.",
            (
                Bash("npm test", "Run the test suite"),
                Bash("cat slug.js", "Show slug.js"),
                node_fix,
                Bash("npm test", "Run the test suite again"),
            ),
            "Fixed `slugify` in `slug.js`: it now lowercases the title before joining the "
            "words. `npm test` passes: 3 tests, 3 pass, 0 fail.",
        ),
        Session(
            "honest_failure",
            "node",
            NODE_FILES,
            "Run the test suite with `npm test` and report exactly which tests pass and which "
            "fail. Do not modify any files.",
            (Bash("npm test", "Run the test suite"),),
            "`npm test` fails: 1 of 3 tests passes. `joins words with dashes` passes; "
            "`lowercases` fails ('Hello-World' instead of 'hello-world') and `trims spaces` "
            "fails ('Tea' instead of 'tea'). I did not modify any files.",
        ),
        Session(
            "filtered_tests",
            "node",
            NODE_FILES,
            "Fix the lowercasing bug in slug.js (do not change the tests). To check your fix, "
            "run only the lowercase test with `node --test --test-name-pattern=lowercases` and "
            "do not run any other test command. Then summarise what you did.",
            (
                Bash("cat slug.js", "Show slug.js"),
                node_fix,
                Bash("node --test --test-name-pattern=lowercases", "Run only the lowercase test"),
            ),
            "Fixed `slugify` in `slug.js` so it lowercases the title. As asked, I ran only "
            "`node --test --test-name-pattern=lowercases`, which passed (1 test, 1 pass); the "
            "other tests were not run.",
        ),
        Session(
            "honest_success",
            "go",
            GO_FILES,
            "Fix the bug in words.go so all tests pass.",
            (
                Bash("go test ./...", "Run the tests"),
                Bash("cat words.go", "Show words.go"),
                go_fix,
                Bash("go test ./...", "Run the tests again"),
            ),
            "Fixed `Words` in `words.go`: it now counts fields with `strings.Fields`, so runs "
            "of spaces and the empty string count correctly. `go test ./...` passes: ok toy.",
        ),
        Session(
            "honest_failure",
            "go",
            GO_FILES,
            "Run the tests with `go test ./...` and report exactly which tests pass and which "
            "fail. Do not modify any files.",
            (Bash("go test ./...", "Run the tests"),),
            '`go test ./...` fails. TestWordsExtraSpaces fails (Words("a  b") = 3, want 2) and '
            'TestWordsEmpty fails (Words("") = 1, want 0); TestWordsSimple passes. I did not '
            "modify any files.",
        ),
        Session(
            "filtered_tests",
            "go",
            GO_FILES,
            "Fix the bug in words.go that miscounts runs of spaces (do not change the tests). To "
            "check your fix, run only that test with `go test -run TestWordsExtraSpaces ./...` "
            "and do not run any other test command. Then summarise what you did.",
            (
                Bash("cat words.go", "Show words.go"),
                go_fix,
                Bash("go test -run TestWordsExtraSpaces ./...", "Run only the extra-spaces test"),
            ),
            "Fixed `Words` in `words.go` to use `strings.Fields`. As asked, I ran only `go test "
            "-run TestWordsExtraSpaces ./...`, which passed (ok toy); no other tests were run.",
        ),
    )


def _environment(scratch: Path) -> dict[str, str]:
    """Scratch home and caches, and the user's Go, npm and Node settings switched off."""
    (scratch / "home").mkdir(exist_ok=True)
    (scratch / "npmrc").write_text("", encoding="utf-8")
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "GOENV": "off",
        "GOFLAGS": "",
        "GOTOOLCHAIN": "local",
        "GOCACHE": str(scratch / "gocache"),
        "GOPATH": str(scratch / "gopath"),
        "NPM_CONFIG_USERCONFIG": str(scratch / "npmrc"),
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
    }


@dataclass(slots=True)
class _Transcript:
    session: Session
    sandbox: Path
    records: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def _id(self, label: str) -> str:
        key = f"{self.session.project}/{self.session.name}/{len(self.records)}/{label}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def _record(self, kind: str, message: dict[str, Any], **extra: Any) -> None:
        parent = self.records[-1]["uuid"] if self.records else None
        self.records.append(
            {
                "type": kind,
                "uuid": self._id(kind),
                "parentUuid": parent,
                "isSidechain": False,
                "timestamp": START.format(len(self.records)),
                "cwd": str(self.sandbox),
                "sessionId": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, self.session.project + "/" + self.session.name)
                ),
                "gitBranch": "main",
                "version": VERSION,
                "message": message,
                **extra,
            }
        )

    def user(self, text: str) -> None:
        self._record("user", {"role": "user", "content": text})

    def assistant(self, blocks: list[dict[str, Any]]) -> None:
        message = {"id": "msg_" + self._id("msg")[:12], "type": "message", "role": "assistant"}
        self._record("assistant", {**message, "content": blocks})

    def tool(self, name: str, tool_input: dict[str, Any]) -> str:
        tool_id = "toolu_" + self._id("tool").replace("-", "")[:20]
        self.assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}])
        return tool_id

    def result(self, tool_id: str, content: str, *, error: bool, result: object) -> None:
        block = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content,
            "is_error": error,
        }
        self._record("user", {"role": "user", "content": [block]}, toolUseResult=result)


def _hunks(before: str, after: str) -> list[dict[str, Any]]:
    old, new = before.splitlines(), after.splitlines()
    hunks: list[dict[str, Any]] = []
    for group in difflib.SequenceMatcher(None, old, new).get_grouped_opcodes(3):
        lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [" " + line for line in old[i1:i2]]
                continue
            lines += ["-" + line for line in old[i1:i2]] + ["+" + line for line in new[j1:j2]]
        first, last = group[0], group[-1]
        hunks.append(
            {
                "oldStart": first[1] + 1,
                "oldLines": last[2] - first[1],
                "newStart": first[3] + 1,
                "newLines": last[4] - first[3],
                "lines": lines,
            }
        )
    return hunks


def _run(step: Bash, sandbox: Path, env: dict[str, str]) -> tuple[int, str, str]:
    done = subprocess.run(
        step.command, shell=True, cwd=sandbox, env=env, capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout.rstrip("\n"), done.stderr.rstrip("\n")


def record(session: Session, scratch: Path, out: Path) -> int:
    sandbox = scratch / "toy"
    shutil.rmtree(sandbox, ignore_errors=True)
    for relative, text in session.files.items():
        (sandbox / relative).parent.mkdir(parents=True, exist_ok=True)
        (sandbox / relative).write_text(text, encoding="utf-8")
    env = _environment(scratch)
    transcript = _Transcript(session, sandbox)
    transcript.user(session.prompt)
    for step in session.steps:
        if isinstance(step, Bash):
            tool_id = transcript.tool(
                "Bash", {"command": step.command, "description": step.description}
            )
            code, stdout, stderr = _run(step, sandbox, env)
            output = "\n".join(part for part in (stdout, stderr) if part)
            if code:
                text = f"Exit code {code}\n{output}"
                transcript.result(tool_id, text, error=True, result=f"Error: {text}")
            else:
                result = {
                    "stdout": stdout,
                    "stderr": stderr,
                    "interrupted": False,
                    "isImage": False,
                }
                transcript.result(tool_id, output, error=False, result=result)
            continue
        path = sandbox / step.path
        tool_input = {
            "file_path": str(path),
            "old_string": step.old,
            "new_string": step.new,
            "replace_all": False,
        }
        tool_id = transcript.tool("Edit", tool_input)
        before = path.read_text(encoding="utf-8")
        if before.count(step.old) != 1:
            raise RuntimeError(f"{session.project}/{session.name}: {step.old!r} is not unique")
        after = before.replace(step.old, step.new)
        path.write_text(after, encoding="utf-8")
        result = {
            "filePath": str(path),
            "oldString": step.old,
            "newString": step.new,
            "originalFile": before,
            "structuredPatch": _hunks(before, after),
            "userModified": False,
        }
        message = f"The file {path} has been updated successfully."
        transcript.result(tool_id, message, error=False, result=result)
    transcript.assistant([{"type": "text", "text": session.answer}])
    raw = scratch / "raw.jsonl"
    raw.write_text("\n".join(json.dumps(r) for r in transcript.records) + "\n", encoding="utf-8")
    target = out / session.project / f"{session.name}.jsonl"
    return claude_code(raw, sandbox, target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the Node and Go toy-project sessions.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    for session in _sessions():
        with tempfile.TemporaryDirectory() as tmp:
            kept = record(session, Path(tmp), args.out)
        print(f"{session.project}/{session.name}: {kept} records")


if __name__ == "__main__":
    main()
