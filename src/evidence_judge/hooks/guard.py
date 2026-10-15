"""A pre-tool hook that refuses writes which would switch off the evidence hooks or the judge.

An agent running as the same OS user as its harness can write `{"disableAllHooks": true}` to
`.claude/settings.local.json`, delete a hook script or replace the `evidence-judge` executable,
and every later hook (the recorder, the Stop judge) is silently gone. This hook runs before each
file write or shell command and denies the ones that clearly target agent configuration, the
judge's install or an evidence store, reusing the session judge's write-target analysis. It is
fast and has no I/O beyond its input; anything it cannot read is allowed (fail open), because
the session judge still reports the write afterwards. Managed settings
(`allowManagedHooksOnly`) are the stronger control: see the integration guides.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, cast

from evidence_judge.sessions import shell

_FILE_KEYS: Final = ("file_path", "notebook_path", "path", "absolute_path", "filePath")
_WRITE_TOOLS: Final = frozenset(
    {"write", "edit", "multiedit", "notebookedit", "write_file", "replace", "create_file",
     "str_replace_editor", "str_replace_based_edit_tool"}
)  # fmt: skip
_PATCH_FILE: Final = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_PATCH_MOVE: Final = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _command(tool_input: Mapping[str, object]) -> str | None:
    command = tool_input.get("command")
    if isinstance(command, list):
        return shell.unwrap_shell([str(part) for part in cast(list[object], command)])
    return _text(command) or _text(tool_input.get("cmd"))


def _path_reason(path: str, content: str) -> str | None:
    why = shell.guard_path_reason(path)
    if why is None and shell.SESSION_STORE.search(path):
        why = f"writes {path}, where the session's evidence is kept"
    return shell.hook_switch_reason(why, content) if why else None


def guard_reason(tool_name: str, tool_input: Mapping[str, object], cwd: str | None) -> str | None:
    """Why the tool call would switch off the evidence hooks or the judge, or None to allow."""
    name = tool_name.casefold()
    command = _command(tool_input)
    if command is not None:
        if name == "apply_patch" or "*** Begin Patch" in command:
            paths = [*_PATCH_FILE.findall(command), *_PATCH_MOVE.findall(command)]
            return next((r for p in paths if (r := _path_reason(p.strip(), command))), None)
        analysis = shell.analyse(command, cwd)
        if analysis.guard_tamper:
            return analysis.guard_tamper
        if analysis.session_store:
            return "writes where the session's evidence is kept"
        return None
    content = " ".join(
        v
        for k in ("content", "new_string", "new_source", "edits")
        if (v := _flat(tool_input.get(k)))
    )
    if name not in _WRITE_TOOLS:
        return None
    for key in _FILE_KEYS:
        if (path := _text(tool_input.get(key))) and (why := _path_reason(path, content)):
            return why
    return None


def _flat(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flat(v) for v in cast(list[object], value))
    if isinstance(value, dict):
        return " ".join(_flat(v) for v in cast(dict[str, object], value).values())
    return ""


def deny_message(why: str) -> str:
    return (
        f"Evidence Judge blocked this call ({why}). Agent configuration, hook definitions, the "
        "evidence-judge install and the evidence logs are off limits during a judged session; "
        "ask the user to make this change if it is really needed."
    )
