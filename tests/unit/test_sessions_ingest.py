from __future__ import annotations

import getpass
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.sessions.ingest import (
    _lines,
    load_codex,
    load_evidence_log,
    load_session,
    load_transcript,
    locate,
)
from evidence_judge.sessions.ingest._lines import LineStats, iter_records
from evidence_judge.sessions.ingest.claude_code import project_slug
from evidence_judge.sessions.models import (
    CallStatus,
    CommandEvent,
    EvidenceKind,
    FileEditEvent,
    FileReadEvent,
    OtherToolEvent,
    SearchEvent,
    SessionSource,
    SubagentEvent,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "sessions"
CWD = "/work/toy"


def _write(path: Path, records: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _base(kind: str, n: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "uuid": f"u{n}",
        "isSidechain": False,
        "timestamp": f"2026-10-15T10:00:{n:02d}Z",
        "cwd": CWD,
        "sessionId": "s-1",
        "version": "2.1.300",
        "gitBranch": "main",
        **extra,
    }


def _use(
    n: int, use_id: str, name: str, tool_input: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    block = {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}
    return _base(
        "assistant", n, message={"id": f"m{n}", "role": "assistant", "content": [block]}, **extra
    )


def _result(
    n: int,
    use_id: str,
    content: Any,
    *,
    is_error: bool | None = None,
    tur: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    record = _base("user", n, message={"role": "user", "content": [block]}, **extra)
    if tur is not None:
        record["toolUseResult"] = tur
    return record


def _say(n: int, text: str, message_id: str | None = None) -> dict[str, Any]:
    return _base(
        "assistant",
        n,
        message={
            "id": message_id or f"m{n}",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    )


def _bash_ok(stdout: str, **extra: Any) -> dict[str, Any]:
    return {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, **extra}


def _commands(run: Any) -> list[CommandEvent]:
    return [e for e in run.events if isinstance(e, CommandEvent)]


def test_real_claude_code_success_session() -> None:
    run = load_transcript(FIXTURES / "claude_code" / "honest_success.jsonl")
    assert run.source is SessionSource.CLAUDE_CODE
    assert run.evidence is EvidenceKind.TRANSCRIPT
    assert run.cwd == CWD
    assert run.harness_version == "2.1.300"
    assert run.complete and run.notes == []
    assert run.task is not None and run.task.startswith("calc.py has a bug")
    assert run.final_answer is not None and run.final_answer.startswith("Fixed.")
    assert [e.id for e in run.events] == ["cmd#1", "cmd#2", "edit#1", "cmd#3"]
    first_run, last_run = _commands(run)[1:]
    assert first_run.summary is not None and first_run.summary.failed == 2
    assert first_run.analysis.exit_masked_by == "its output is piped into tail"
    assert last_run.exit_code == 0 and last_run.status is CallStatus.OK
    assert last_run.summary is not None and last_run.summary.green
    edit = run.events[2]
    assert isinstance(edit, FileEditEvent)
    assert (edit.path, edit.change, edit.lines_added, edit.lines_removed) == (
        "calc.py",
        "modify",
        1,
        1,
    )


def test_real_claude_code_failure_session_exit_codes() -> None:
    run = load_transcript(FIXTURES / "claude_code" / "honest_failure.jsonl")
    denied, failed = _commands(run)
    assert denied.status is CallStatus.DENIED and denied.exit_code is None
    assert failed.status is CallStatus.ERROR and failed.exit_code == 1
    assert not failed.output.startswith("Exit code")
    assert failed.summary is not None and failed.summary.failed == 2


def test_transcript_reads_only_conversation_records(tmp_path: Path) -> None:
    use = _use(1, "t1", "Bash", {"command": "pytest -q"})
    path = _write(
        tmp_path / "s.jsonl",
        [
            _base("user", 0, message={"role": "user", "content": "Fix it"}),
            use,
            {"type": "api-request-blob", "message": use["message"]},
            {"type": "attachment", "attachment": {"type": "prompt_snapshot"}},
            {"type": "brand-new-record", "x": 1},
            "not json",
            "[1, 2]",
            _result(2, "t1", "3 passed in 0.1s", is_error=False, tur=_bash_ok("3 passed in 0.1s")),
            _say(3, "All tests pass."),
        ],
    )
    run = load_transcript(path)
    assert len(_commands(run)) == 1
    assert run.task == "Fix it"
    assert not run.complete
    assert "1 line(s) were not valid JSON and were skipped" in run.notes
    assert "1 line(s) were not JSON objects and were skipped" in run.notes
    assert "skipped 1 record(s) of unknown type 'brand-new-record'" in run.notes


@pytest.mark.parametrize(
    ("block", "tur", "status", "exit_code", "truncated", "background"),
    [
        ({"content": "ok", "is_error": False}, _bash_ok("ok"), CallStatus.OK, 0, False, False),
        (
            {"content": "Exit code 2\nboom", "is_error": True},
            "Error: Exit code 2\nboom",
            CallStatus.ERROR,
            2,
            False,
            False,
        ),
        (
            {"content": "no matches", "is_error": False},
            _bash_ok("", returnCodeInterpretation="No matches found"),
            CallStatus.OK,
            None,
            False,
            False,
        ),
        (
            {"content": "x", "is_error": False},
            _bash_ok("x", interrupted=True),
            CallStatus.INTERRUPTED,
            None,
            False,
            False,
        ),
        (
            {"content": "Command running in background with ID: b1", "is_error": False},
            _bash_ok("", backgroundTaskId="b1"),
            CallStatus.OK,
            None,
            True,
            True,
        ),
        (
            {"content": "<persisted-output>preview</persisted-output>", "is_error": False},
            _bash_ok("preview", persistedOutputPath="/x", persistedOutputSize=10),
            CallStatus.OK,
            0,
            True,
            False,
        ),
        (
            {"content": "Permission to use Bash has been denied.", "is_error": True},
            None,
            CallStatus.DENIED,
            None,
            False,
            False,
        ),
        (
            {"content": "something odd", "is_error": True},
            None,
            CallStatus.ERROR,
            None,
            False,
            False,
        ),
        (
            {"content": "Exit code 1\ntimed out", "is_error": True},
            _bash_ok("", timedOutAfterMs=1000),
            CallStatus.ERROR,
            1,
            False,
            False,
        ),
        (
            {"content": "timed out", "is_error": True},
            _bash_ok("", timedOutAfterMs=1000),
            CallStatus.INTERRUPTED,
            None,
            False,
            False,
        ),
        ({"content": "ok"}, None, CallStatus.OK, None, False, False),
    ],
)
def test_bash_exit_status_inference(
    tmp_path: Path,
    block: dict[str, Any],
    tur: Any,
    status: CallStatus,
    exit_code: int | None,
    truncated: bool,
    background: bool,
) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _use(1, "t1", "Bash", {"command": "pytest"}),
            _result(2, "t1", block["content"], is_error=block.get("is_error"), tur=tur),
        ],
    )
    (command,) = _commands(load_transcript(path))
    assert (command.status, command.exit_code) == (status, exit_code)
    assert command.output_truncated is truncated
    assert command.background is background


def test_edit_tools(tmp_path: Path) -> None:
    patch = [
        {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a = 1", "+a = 2"]}
    ]
    path = _write(
        tmp_path / "s.jsonl",
        [
            _use(1, "w1", "Write", {"file_path": f"{CWD}/new.py", "content": "x = 1\n"}),
            _result(2, "w1", "created", tur={"type": "create", "filePath": f"{CWD}/new.py"}),
            _use(3, "w2", "Write", {"file_path": f"{CWD}/old.py", "content": "a = 2\n"}),
            _result(4, "w2", "updated", tur={"type": "update", "structuredPatch": patch}),
            _use(
                5,
                "m1",
                "MultiEdit",
                {
                    "file_path": f"{CWD}/tests/test_a.py",
                    "edits": [
                        {"old_string": "    assert f() == 1", "new_string": "    assert f() == 2"},
                        "junk",
                    ],
                },
            ),
            _result(6, "m1", "ok"),
            _use(
                7,
                "e1",
                "Edit",
                {"file_path": f"{CWD}/c.py", "old_string": "zz", "new_string": "yy"},
            ),
            _result(
                8,
                "e1",
                "<tool_use_error>String to replace not found</tool_use_error>",
                is_error=True,
            ),
            _use(
                9,
                "n1",
                "NotebookEdit",
                {
                    "notebook_path": f"{CWD}/n.ipynb",
                    "new_source": "print(1)",
                    "edit_mode": "insert",
                },
            ),
            _result(10, "n1", "ok"),
            _use(11, "w3", "Write", {"file_path": "/elsewhere/x.py", "content": "q"}),
            _result(12, "w3", "ok", tur={"type": "update", "originalFile": "p\nq"}),
            _use(
                13, "e2", "Edit", {"file_path": f"{CWD}/u.py", "old_string": "a", "new_string": "b"}
            ),
            _result(14, "e2", "ok", tur={"userModified": True}),
        ],
    )
    run = load_transcript(path)
    edits = [e for e in run.events if isinstance(e, FileEditEvent)]
    assert [(e.path, e.change) for e in edits] == [
        ("new.py", "add"),
        ("old.py", "modify"),
        ("tests/test_a.py", "modify"),
        ("c.py", "modify"),
        ("n.ipynb", "modify"),
        ("/elsewhere/x.py", "modify"),
        ("u.py", "modify"),
    ]
    assert edits[1].lines_removed == 1 and edits[1].lines_added == 1
    assert edits[2].test_file and edits[2].test_findings
    assert edits[3].status is CallStatus.DENIED and edits[3].test_findings == []
    assert edits[5].lines_removed == 1
    assert edits[6].by_user


def test_other_tools_and_missing_results(tmp_path: Path) -> None:
    sidechain = _use(7, "s1", "Bash", {"command": "ls"}, isSidechain=True)
    path = _write(
        tmp_path / "s.jsonl",
        [
            _use(1, "r1", "Read", {"file_path": f"{CWD}/a.py"}),
            _result(2, "r1", "content"),
            _use(3, "g1", "Grep", {"pattern": "def add"}),
            _result(4, "g1", "a.py"),
            _use(
                5,
                "a1",
                "Task",
                {"description": "explore", "subagent_type": "Explore", "prompt": "p"},
            ),
            _result(6, "a1", "done"),
            sidechain,
            _result(8, "s1", "a.py", is_error=False, tur=_bash_ok("a.py"), isSidechain=True),
            _use(9, "x1", "mcp__fs__write", {"path": "a"}),
            _result(10, "x1", "ok"),
            _use(11, "t1", "TodoWrite", {"todos": []}),
            _result(12, "t1", "ok"),
            _use(13, "b1", "Bash", {"command": "pytest"}),
            _base(
                "assistant",
                14,
                message={
                    "id": "m14",
                    "role": "assistant",
                    "content": [{"type": "server_tool_use"}, {"type": "thinking", "thinking": "x"}],
                },
            ),
        ],
    )
    run = load_transcript(path)
    kinds = [type(e).__name__ for e in run.events]
    assert kinds == [
        "FileReadEvent",
        "SearchEvent",
        "SubagentEvent",
        "CommandEvent",
        "OtherToolEvent",
        "OtherToolEvent",
        "CommandEvent",
    ]
    assert isinstance(run.events[0], FileReadEvent) and run.events[0].path == "a.py"
    assert isinstance(run.events[1], SearchEvent) and run.events[1].pattern == "def add"
    assert isinstance(run.events[2], SubagentEvent) and run.events[2].subagent_type == "Explore"
    assert run.events[3].agent_id == "sidechain"
    mcp, todo = run.events[4], run.events[5]
    assert isinstance(mcp, OtherToolEvent) and mcp.may_mutate
    assert isinstance(todo, OtherToolEvent) and not todo.may_mutate
    last = run.events[6]
    assert isinstance(last, CommandEvent) and last.status is CallStatus.NO_RESULT
    assert "1 tool call(s) have no recorded result" in run.notes
    assert "skipped 1 content block(s) of unknown type 'server_tool_use'" in run.notes


def test_final_answer_is_the_last_main_chain_message(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _base(
                "user",
                0,
                message={"role": "user", "content": "<command-name>/clear</command-name>"},
            ),
            _base("user", 1, isMeta=True, message={"role": "user", "content": "meta"}),
            _base(
                "user",
                2,
                message={"role": "user", "content": [{"type": "text", "text": "Real task"}]},
            ),
            _say(3, "Working on it."),
            _say(4, "Part one.", message_id="final"),
            _say(5, "Part two.", message_id="final"),
            _base(
                "assistant",
                6,
                isSidechain=True,
                message={
                    "id": "side",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "subagent says done"}],
                },
            ),
        ],
    )
    run = load_transcript(path)
    assert run.task == "Real task"
    assert run.final_answer == "Part one.\n\nPart two."


def test_locate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    project = tmp_path / "repo"
    project.mkdir()
    folder = root / project_slug(project.resolve())
    folder.mkdir(parents=True)
    old = folder / "aaaa-1111.jsonl"
    new = folder / "bbbb-2222.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    import os

    os.utime(old, (1, 1))
    assert locate("latest", project=project, root=root) == new
    assert locate("aaaa-1111", root=root) == old
    assert locate(str(new)) == new
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert locate("aaaa-1111") == old
    with pytest.raises(FileNotFoundError):
        locate("missing-id", root=root)
    with pytest.raises(FileNotFoundError):
        locate("latest", project=tmp_path, root=root)
    with pytest.raises(FileNotFoundError):
        locate(str(tmp_path / "nope.jsonl"))
    with pytest.raises(FileNotFoundError):
        locate("bad id!", root=root)


def _log_line(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": 1,
        "agent": "claude-code",
        "ts": "2026-10-15T10:00:00Z",
        "session_id": "s-1",
        "tool": "Bash",
        "input": {"command": "pytest -q"},
        "exit_code": 0,
        "stdout_sha256": None,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "cwd": CWD,
    }
    base.update(fields)
    return base


def _chain(records: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    previous: str | None = None
    for seq, record in enumerate(records):
        line = json.dumps({**record, "seq": seq, "prev": previous})
        previous = hashlib.sha256(line.encode()).hexdigest()
        lines.append(line)
    return lines


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_evidence_log_with_hash_chain(tmp_path: Path) -> None:
    out = "...\n3 passed in 0.01s"
    path = _write(
        tmp_path / "e.jsonl",
        _chain(
            [
                {
                    "v": 1,
                    "agent": "claude-code",
                    "ts": "2026-10-15T10:00:00Z",
                    "session_id": "s-1",
                    "event": "prompt",
                    "text": "Fix it",
                },
                _log_line(
                    tool="Edit",
                    input={
                        "file_path": f"{CWD}/calc.py",
                        "old_string": "a - b",
                        "new_string": "a + b",
                    },
                    exit_code=None,
                ),
                _log_line(stdout_excerpt=out, stdout_sha256=_sha(out), tool_use_id="t1"),
                {
                    "v": 1,
                    "agent": "claude-code",
                    "ts": "2026-10-15T10:00:09Z",
                    "session_id": "s-1",
                    "event": "stop",
                    "last_assistant_message": "All tests pass.",
                },
                {
                    "v": 1,
                    "agent": "claude-code",
                    "ts": "2026-10-15T10:00:09Z",
                    "session_id": "s-1",
                    "event": "subagent_stop",
                },
            ]
        ),
    )
    run = load_evidence_log(path)
    assert run.evidence is EvidenceKind.EVIDENCE_LOG
    assert run.complete, run.notes
    assert run.task == "Fix it"
    assert run.final_answer == "All tests pass."
    command = _commands(run)[0]
    assert command.exit_code == 0 and not command.output_truncated
    assert command.summary is not None and command.summary.passed == 3


def test_evidence_log_detects_a_broken_chain(tmp_path: Path) -> None:
    lines = _chain([_log_line(), _log_line(exit_code=1)])
    tampered = json.loads(lines[0])
    tampered["exit_code"] = 5
    lines[0] = json.dumps(tampered)
    run = load_evidence_log(_write(tmp_path / "e.jsonl", lines))
    assert not run.complete
    assert any("hash chain broken" in n for n in run.notes)
    gap = _chain([_log_line(), _log_line()])
    gap_record = json.loads(gap[1])
    gap_record["seq"] = 5
    gap[1] = json.dumps(gap_record)
    assert not load_evidence_log(_write(tmp_path / "g.jsonl", gap)).complete


def test_evidence_log_problems_name_physical_lines(tmp_path: Path) -> None:
    missing = _log_line()
    del missing["cwd"]
    run = load_evidence_log(
        _write(tmp_path / "e.jsonl", ["not json", "", missing, _log_line(v=2), _log_line()])
    )
    assert "line 3: missing cwd" in run.notes
    assert "line 4: unsupported version 2" in run.notes
    gap = _chain([_log_line(), _log_line()])
    gap_record = json.loads(gap[1])
    gap_record["seq"] = 5
    chained = load_evidence_log(_write(tmp_path / "g.jsonl", ["{", gap[0], json.dumps(gap_record)]))
    assert "hash chain broken: line 3 has seq 5, expected 1" in chained.notes


def test_evidence_log_without_chain_and_bad_records(tmp_path: Path) -> None:
    missing = _log_line()
    del missing["cwd"]
    path = _write(
        tmp_path / "e.jsonl",
        [
            _log_line(stdout_excerpt="partial", stdout_sha256=_sha("full output")),
            _log_line(stdout_excerpt="x", output_truncated=False, status="denied"),
            missing,
            _log_line(v=2),
            _log_line(agent="mystery"),
            {**_log_line(), "event": "weird"},
        ],
    )
    run = load_evidence_log(path)
    assert not run.complete
    first, second = _commands(run)[:2]
    assert first.output_truncated
    assert not second.output_truncated and second.status is CallStatus.DENIED
    assert any("no seq/prev hash chain" in n for n in run.notes)
    assert any("missing cwd" in n for n in run.notes)
    assert any("unsupported version 2" in n for n in run.notes)
    assert any("unknown agent 'mystery'" in n for n in run.notes)


class _ReadSizes:
    def __init__(self, handle: Any, sizes: list[int]) -> None:
        self.handle = handle
        self.sizes = sizes

    def __enter__(self) -> _ReadSizes:
        return self

    def __exit__(self, *exc: object) -> None:
        self.handle.close()

    def readline(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        return self.handle.readline(size)


def test_an_over_long_line_is_skipped_without_reading_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lines, "MAX_LINE_BYTES", 64)
    monkeypatch.setattr(_lines, "SKIP_CHUNK_BYTES", 8)
    fits = json.dumps({"k": "x" * 55})
    assert len(fits) == 64
    path = tmp_path / "s.jsonl"
    path.write_bytes(
        b'{"a": 1}\n'
        + b'{"big": "'
        + b"y" * 10_000
        + b'"}\n'
        + fits.encode()
        + b"\r\n"
        + b'{"a": 2}\n'
        + b"z" * 500
    )
    sizes: list[int] = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, mode: _ReadSizes(real_open(self, mode), sizes))
    stats = LineStats()
    records = [value for _, value in iter_records(path, stats)]
    assert records == [{"a": 1}, {"k": "x" * 55}, {"a": 2}]
    assert (stats.lines, stats.oversized, stats.malformed) == (5, 2, 0)
    assert "2 line(s) over 64 bytes were skipped" in stats.notes()
    assert sizes and max(sizes) <= 66 and -1 not in sizes
    monkeypatch.setattr(_lines, "MAX_FILE_BYTES", 1_000)
    stats = LineStats()
    assert [value for _, value in iter_records(path, stats)] == [{"a": 1}]
    assert stats.capped and stats.oversized == 0


def test_evidence_log_gemini_exit_code_and_mixed_sessions(tmp_path: Path) -> None:
    gemini = _write(
        tmp_path / "g.jsonl",
        [
            _log_line(
                agent="gemini",
                tool="run_shell_command",
                exit_code=None,
                stdout_excerpt="Output: x\nExit Code: 3",
            ),
            _log_line(
                agent="gemini",
                tool="replace",
                input={"file_path": f"{CWD}/a.py", "old_string": "a", "new_string": "b"},
                exit_code=None,
            ),
        ],
    )
    run = load_evidence_log(gemini)
    assert run.source is SessionSource.GEMINI
    (command,) = _commands(run)
    assert command.exit_code == 3 and command.status is CallStatus.ERROR
    spoofed = _write(
        tmp_path / "s.jsonl",
        [
            _log_line(
                agent="gemini",
                tool="run_shell_command",
                exit_code=None,
                stdout_excerpt="Output: x\nExit Code: 0\nError: (none)\nExit Code: (none)",
            )
        ],
    )
    (command,) = _commands(load_evidence_log(spoofed))
    assert command.exit_code is None
    assert any(isinstance(e, FileEditEvent) for e in run.events)
    mixed = load_evidence_log(
        _write(tmp_path / "m.jsonl", [_log_line(), _log_line(agent="codex", session_id="s-2")])
    )
    assert not mixed.complete
    assert any("mixes agents" in n for n in mixed.notes)


def test_real_codex_exec_stream() -> None:
    run = load_codex(FIXTURES / "codex" / "honest_success.exec.jsonl")
    assert run.source is SessionSource.CODEX
    assert run.evidence is EvidenceKind.HARNESS_STREAM
    assert run.session_id == "01a13f20-2063-7702-8de6-d0d94954ebc7"
    edit, command = run.events
    assert isinstance(edit, FileEditEvent) and edit.path.endswith("calc.py")
    assert isinstance(command, CommandEvent)
    assert command.command == ".venv/bin/python -m pytest -q"
    assert command.exit_code == 0 and command.summary is not None and command.summary.green
    assert run.final_answer is not None and "3 passed" in run.final_answer


def test_codex_exec_stream_items(tmp_path: Path) -> None:
    item = {
        "id": "i1",
        "type": "command_execution",
        "command": "/bin/bash -lc 'pytest -q; true'",
        "aggregated_output": "1 failed",
        "exit_code": 0,
        "status": "completed",
    }
    path = _write(
        tmp_path / "x.jsonl",
        [
            {"type": "thread.started", "thread_id": "th"},
            {"type": "item.completed", "item": item},
            {"type": "item.completed", "item": item},
            {
                "type": "item.completed",
                "item": {
                    "id": "i2",
                    "type": "command_execution",
                    "command": "rm -rf x",
                    "status": "declined",
                    "exit_code": None,
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "i3", "type": "mcp_tool_call", "tool": "write"},
            },
            {"type": "item.completed", "item": {"id": "i4", "type": "web_search"}},
            {
                "type": "item.completed",
                "item": {
                    "id": "i5",
                    "type": "file_change",
                    "changes": [
                        {"path": "tests/test_a.py", "kind": "delete"},
                        {"path": "b.py", "kind": "add"},
                    ],
                    "status": "completed",
                },
            },
            {"type": "turn.failed", "error": {"message": "rate limited"}},
            {"type": "something.new"},
        ],
    )
    run = load_codex(path)
    commands = _commands(run)
    assert len(commands) == 2
    assert commands[0].command == "pytest -q; true"
    assert commands[0].analysis.exit_masked_by is not None
    assert commands[1].status is CallStatus.DENIED
    edits = [e for e in run.events if isinstance(e, FileEditEvent)]
    assert [(e.path, e.change) for e in edits] == [("tests/test_a.py", "delete"), ("b.py", "add")]
    assert edits[0].test_findings == ["deleted test file test_a.py"]
    assert "the turn failed: rate limited" in run.notes
    assert any(isinstance(e, OtherToolEvent) and e.name == "mcp:write" for e in run.events)


def test_codex_rollout_both_generations(tmp_path: Path) -> None:
    diff = "@@ -1 +1 @@\n-    assert add(2, 3) == 5\n+    assert add(2, 3) == -1\n"
    path = _write(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": "2026-10-15T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "r-1", "cwd": CWD, "cli_version": "0.162.1"},
            },
            {
                "timestamp": "2026-10-15T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "child", "parent_thread_id": "r-1"},
            },
            {
                "timestamp": "2026-10-15T10:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Fix calc"},
            },
            {
                "timestamp": "2026-10-15T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "command": ["bash", "-lc", "pytest -q"],
                    "cwd": CWD,
                    "exit_code": 1,
                    "aggregated_output": "== 2 failed, 1 passed in 0.1s ==",
                    "status": "completed",
                },
            },
            {
                "timestamp": "2026-10-15T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "success": True,
                    "changes": {
                        f"{CWD}/calc.py": {
                            "update": {"unified_diff": "-a - b\n+a + b", "move_path": None}
                        }
                    },
                },
            },
            {
                "timestamp": "2026-10-15T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "status": "completed",
                        "changes": {
                            f"{CWD}/tests/test_calc.py": {
                                "type": "update",
                                "unified_diff": diff,
                                "move_path": None,
                            },
                            f"{CWD}/new.py": {"type": "add", "content": "x = 1"},
                            f"{CWD}/gone.py": {"type": "delete"},
                            f"{CWD}/a.py": {"type": "update", "move_path": f"{CWD}/b.py"},
                        },
                    },
                },
            },
            {
                "timestamp": "2026-10-15T10:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["/bin/zsh", "-lc", "pytest -q"],
                        "cwd": CWD,
                        "exit_code": 0,
                        "aggregated_output": "3 passed in 0.1s",
                        "status": "completed",
                    },
                },
            },
            {
                "timestamp": "2026-10-15T10:00:05Z",
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": "McpToolCall", "tool": "x"}},
            },
            {
                "timestamp": "2026-10-15T10:00:05Z",
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": "SubAgentActivity"}},
            },
            {
                "timestamp": "2026-10-15T10:00:06Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "draft"},
            },
            {
                "timestamp": "2026-10-15T10:00:07Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "All tests pass."},
            },
            {
                "timestamp": "2026-10-15T10:00:07Z",
                "type": "response_item",
                "payload": {"type": "message"},
            },
            {"timestamp": "2026-10-15T10:00:07Z", "type": "turn_context", "payload": {"cwd": CWD}},
            {"type": "future_thing"},
        ],
    )
    run = load_codex(path)
    assert run.evidence is EvidenceKind.TRANSCRIPT
    assert (run.session_id, run.cwd, run.harness_version) == ("r-1", CWD, "0.162.1")
    assert run.task == "Fix calc" and run.final_answer == "All tests pass."
    commands = _commands(run)
    assert [c.exit_code for c in commands] == [1, 0]
    assert commands[0].command == "pytest -q"
    edits = [e for e in run.events if isinstance(e, FileEditEvent)]
    assert [(e.path, e.change) for e in edits] == [
        ("calc.py", "modify"),
        ("tests/test_calc.py", "modify"),
        ("new.py", "add"),
        ("gone.py", "delete"),
        ("a.py", "modify"),
        ("a.py", "delete"),
        ("b.py", "add"),
    ]
    assert edits[1].test_findings
    assert any(isinstance(e, SubagentEvent) for e in run.events)


def test_load_session_combines_sources(tmp_path: Path) -> None:
    transcript = FIXTURES / "claude_code" / "honest_success.jsonl"
    log = _write(tmp_path / "e.jsonl", [_log_line(stdout_excerpt="3 passed in 0.1s")])
    run = load_session(claude_code=transcript, evidence_log=log)
    assert run.evidence is EvidenceKind.EVIDENCE_LOG
    assert run.final_answer is not None and run.final_answer.startswith("Fixed.")
    assert run.task is not None and len(run.events) == 1
    codex = load_session(codex=FIXTURES / "codex" / "honest_success.exec.jsonl", answer="Done.")
    assert codex.final_answer == "Done."
    with_codex = load_session(
        codex=FIXTURES / "codex" / "honest_success.exec.jsonl", evidence_log=log
    )
    assert with_codex.final_answer is not None
    with pytest.raises(ValueError, match="no session"):
        load_session()


@pytest.mark.parametrize("fixture", sorted(FIXTURES.rglob("*.jsonl")), ids=lambda p: p.name)
def test_session_fixtures_hold_no_local_or_private_data(fixture: Path) -> None:
    text = fixture.read_text(encoding="utf-8")
    for needle in (str(Path.home()), "/Users/", "/private/", ".claude/projects"):
        assert needle not in text
    user = re.escape(getpass.getuser())
    assert not re.search(rf"(?<![\w/:.-]){user}(?![\w/.-])", text)
    kinds = {json.loads(line).get("type") for line in text.splitlines()}
    assert not kinds & {"attachment", "api-request", "api-request-blob", "system"}


def test_codex_listed_changes_with_diffs(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": "2026-10-15T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "r-2", "cwd": CWD},
            },
            {
                "timestamp": "2026-10-15T10:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "status": "completed",
                        "changes": [
                            {
                                "path": f"{CWD}/calc.py",
                                "kind": {"type": "update", "move_path": None},
                                "diff": "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n"
                                "+    return a + b\n",
                            },
                            {"path": f"{CWD}/new.py", "kind": {"type": "add"}, "diff": "X = 1\n"},
                            {"path": f"{CWD}/old.py", "kind": "delete", "diff": "Y = 2\n"},
                            {"path": f"{CWD}/blind.py", "kind": "update"},
                        ],
                    },
                },
            },
        ],
    )
    run = load_codex(path)
    edits = [e for e in run.events if isinstance(e, FileEditEvent)]
    assert [(e.path, e.change, e.lines_removed, e.lines_added) for e in edits] == [
        ("calc.py", "modify", 1, 1),
        ("new.py", "add", 0, 1),
        ("old.py", "delete", 1, 0),
        ("blind.py", "modify", None, None),
    ]
    assert [bool(e.test_findings) for e in edits] == [False, False, False, True]


def test_codex_exec_stream_notes_edits_without_a_diff() -> None:
    run = load_codex(FIXTURES / "codex" / "honest_success.exec.jsonl")
    assert any("records no diff for /work/toy/calc.py" in note for note in run.notes)
    (edit,) = [e for e in run.events if isinstance(e, FileEditEvent)]
    assert edit.test_findings and "with no recorded diff" in edit.test_findings[0]
