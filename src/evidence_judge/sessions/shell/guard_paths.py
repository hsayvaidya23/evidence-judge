"""Session stores, agent configuration and the judge's install: what can switch off the hooks."""

from __future__ import annotations

import re
from typing import Final

from evidence_judge.sessions.shell.lexer import Pipeline, program_name, unspelled, unwrap

SESSION_STORE: Final = re.compile(
    r"\.claude/projects|\.claude/plugins|\.codex/sessions|\.codex/archived_sessions|"
    r"\.gemini/tmp|session-evidence|evidence-judge/evidence|\.evidence-judge(?:/|$)|"
    r"EVJ_HOOK_LOG_DIR",
    re.IGNORECASE,
)
"""Where harnesses and the evidence recorder keep session records (and the project's
`.evidence-judge/` spec): a command that writes there can rewrite the evidence."""


AGENT_CONFIG: Final = re.compile(
    r"(?:^|/)\.claude/(?:[^/]*settings[^/]*\.json|(?:[^/]+/)*[^/]*hooks[^/]*)$|"
    r"(?:^|/)\.claude/(?:hooks|plugins)(?:/|$)|(?:^|/)\.claude-plugin(?:/|$)|"
    r"(?:^|/)\.codex(?:/|$)|(?:^|/)\.gemini(?:/|$)|(?:^|/)hooks/hooks\.json$|"
    r"(?:^|/)\.mcp\.json$|(?:^|/)managed-settings\.json$|"
    r"(?:^|/)(?:ClaudeCode|claude-code|codex|gemini-cli)/(?:[^/]+/)*[^/]*settings[^/]*$",
    re.IGNORECASE,
)
"""Coding-agent configuration: settings that can turn hooks off (`disableAllHooks`), hook and
plugin definitions, MCP server lists, and the Codex and Gemini CLI homes. An agent that writes
one can switch off the hooks that record and judge it."""


JUDGE_INSTALL: Final = re.compile(
    r"(?:^|/)(?:bin|Scripts)/evidence-judge(?:\.exe|\.cmd)?$|"
    r"(?:^|/)(?:uv/tools|pipx/venvs|tools)/evidence-judge(?:/|$)",
    re.IGNORECASE,
)
"""The installed `evidence-judge` executable or tool environment the hooks run."""


JUDGE_NAME: Final = re.compile(r"evidence[-_]judge", re.IGNORECASE)


_PACKAGE_VERBS: Final = frozenset({"install", "uninstall", "remove", "rm", "upgrade", "reinstall"})


def guard_path_reason(path: str, shown: str | None = None) -> str | None:
    """Why writing `path` (named `shown` in the reason) can disable the evidence hooks or the
    judge, or None."""
    shown = shown or path
    if JUDGE_INSTALL.search(path):
        return f"replaced or removed the evidence-judge install at {shown}"
    if AGENT_CONFIG.search(path):
        return f"changed agent configuration {shown}, which can disable the evidence hooks"
    return None


_HOOK_SWITCH: Final = re.compile(
    r"disableAllHooks|allowManagedHooksOnly|\"?hooks\"?\s*[:=]\s*(?:\{\s*\}|\[\s*\]|null|false)",
    re.IGNORECASE,
)


def hook_switch_reason(why: str, text: str) -> str:
    """`why`, saying so when `text` (a command line or written content) switches hooks off."""
    found = _HOOK_SWITCH.search(text)
    if found is None:
        return why
    return f"{why}; it sets {found.group(0)}, which turns the hooks (the evidence recorder and judge) off"


def writes_session_store(
    pipelines: list[Pipeline],
    command: str,
    touched: list[str],
    deleted: list[str],
    opaque: str | None,
) -> bool:
    """The command writes a session store: it names one as a target, or it names one at all
    (quotes removed, so `~/.cl""aude` counts) and may write files it does not name. Reading
    one (`cat ~/.claude/projects/x.jsonl`) does not count."""
    if any(SESSION_STORE.search(path) for path in [*touched, *deleted]):
        return True
    words = [w for p in pipelines for c in p.commands for w in [*c.words, *c.writes]]
    named = SESSION_STORE.search(command) is not None or any(SESSION_STORE.search(w) for w in words)
    return named and opaque is not None


def guard_tamper(
    pipelines: list[Pipeline],
    command: str,
    touched: list[str],
    deleted: list[str],
    opaque: str | None,
) -> str | None:
    """Why the command can switch off the evidence hooks or the judge they run: it writes or
    deletes agent configuration or the judge's install, (un)installs the judge, or names one
    of those places and may write files it does not name."""
    for path in [*touched, *deleted]:
        if why := guard_path_reason(path):
            return hook_switch_reason(why, command)
        if JUDGE_NAME.search(path) and unspelled(path):
            return f"writes {path}, which can resolve to the evidence-judge executable"
    for pipeline in pipelines:
        for simple in pipeline.commands:
            words = unwrap(list(simple.words))
            if (
                words
                and any(w in _PACKAGE_VERBS for w in words[1:])
                and any(JUDGE_NAME.search(w) for w in words[1:])
            ):
                return f"{program_name(words[0])} changes the evidence-judge install the hooks run"
    if opaque is not None:
        words = [w for p in pipelines for c in p.commands for w in [*c.words, *c.writes]]
        if any(AGENT_CONFIG.search(w) or JUDGE_INSTALL.search(w) for w in words):
            return f"names agent configuration or the judge's install and {opaque}"
    return None
