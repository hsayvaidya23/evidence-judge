"""Credentials passed on a command line: which argument of which tool holds a secret.

A command is split into shell words with their offsets, each word unquoted with ``shlex`` (or
kept as written when its quoting is unbalanced), then matched against per-tool flag tables. A
backslash before a line break joins the lines as a shell does, and a quote left open runs to the
end of its line. A long command is read against its tool's flag tables in windows of
``MAX_COMMAND_WORDS`` words that overlap by ``WINDOW_OVERLAP``, so a flag and its value on either
side of a cut are read together and the cost stays linear.
"""

from __future__ import annotations

import re
import shlex
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from evidence_judge.redact.detectors import (
    is_secret_param,
    looks_like_code_reference,
    looks_like_secret,
)

_MYSQL_TOOLS = frozenset(
    {"mysql", "mysqldump", "mysqladmin", "mysqlimport", "mysqlshow", "mariadb", "mariadb-dump"}
)
"""Tools whose ``-p`` takes the password only when attached: ``mysql -p db`` prompts."""
_LOGIN_TOOLS = frozenset({"docker", "podman", "nerdctl", "buildah", "skopeo", "helm", "oras"})
"""Tools whose ``login`` subcommand takes the password as ``-p``."""
_CONFIG_TOOLS = frozenset({"npm", "yarn", "pnpm", "aws"})
"""Tools whose ``config set <key> <value>`` stores a credential under a secret-named key."""
_TOOLS = _MYSQL_TOOLS | _LOGIN_TOOLS | _CONFIG_TOOLS | {"curl", "sshpass", "htpasswd"}
_CURL_USER_FLAGS = ("--proxy-user", "--user", "-U", "-u")
_CONFIG_VERBS = frozenset({"config", "configure"})
_HTPASSWD_VALUE_FLAGS = frozenset({"-C"})
_SECRET_LONG_FLAGS = frozenset(
    {"password", "passwd", "pwd", "token", "secret", "apikey", "api_key", "otp"}
)
_SECRET_LONG_FLAG_SUFFIXES = (
    "_token",
    "_secret",
    "_password",
    "_api_key",
    "_apikey",
    "_secret_key",
)
_BOOLEAN_FLAG_PREFIXES = ("no_", "with_", "without_", "use_", "skip_", "ask_", "prompt_", "show_")
"""``--no-password`` or ``--with-token`` switch a behaviour on or off and take no value."""
MAX_COMMAND_WORDS = 1024
"""Words of one command read against its tool's flag tables in one window."""
WINDOW_OVERLAP = 2
"""Words a window shares with the one before it, so no flag is cut from its value."""
_COUNT_MAX_DIGITS = 3
"""A number this short after a secret-named flag is a count (``--password 2 failed``)."""
_SPACE = r"(?:[ \t]|\\\r?\n)+"
_WORD = r"""(?:'[^'\r\n]*'?|"(?:[^"\\\r\n]|\\[^\r\n])*"?|\\[^\r\n]|[^\s'"\\|;&<>`()])+"""
_WORD_RE = re.compile(_WORD)
_TOOL_NAMES = "|".join(sorted(map(re.escape, _TOOLS), key=len, reverse=True))
COMMAND_RE = re.compile(rf"(?<![\w.\\=:@-])({_TOOL_NAMES})(?![\w./-])((?:{_SPACE}{_WORD})++)")
"""A known tool's name and the words after it, up to the end of its shell command."""
LONG_FLAG_RE = re.compile(rf"(?<![\w-])(--[A-Za-z][\w-]{{0,63}})({_SPACE})({_WORD})")
"""``--name value`` on any command line, for flags named like a credential."""
_SEGMENT_START_RE = re.compile(r"[;|&`(]|(?<!\\)(?<!\\\r)\n")
_QUOTES = str.maketrans("", "", "'\"")


@dataclass(frozen=True)
class Secret:
    """A secret at ``text[start:end]``, written there as ``raw``; ``prefix`` and its token
    replace it."""

    start: int
    end: int
    value: str
    raw: str
    prefix: str = ""

    def after(self, lead: str) -> Secret:
        """The part of this word after ``lead`` (``-p`` in ``-pX``, ``--user=`` in ``--user=X``)."""
        value = self.value[len(lead) :]
        if self.raw.startswith(lead):
            return Secret(self.start + len(lead), self.end, value, self.raw[len(lead) :])
        return Secret(self.start, self.end, value, self.raw, prefix=self.prefix + lead)


def _unquote(raw: str) -> str | None:
    """A shell word without its quoting, or None when a quote in it is never closed."""
    try:
        return "".join(shlex.split(raw))
    except ValueError:
        return None


def _split_words(text: str) -> list[Secret]:
    words: list[Secret] = []
    for m in _WORD_RE.finditer(text):
        raw = m.group(0)
        value = _unquote(raw)
        words.append(Secret(m.start(), m.end(), raw if value is None else value, raw))
    return words


def _is_value(value: str) -> bool:
    """A flag's value that is itself the secret, not another flag or a variable reference."""
    return bool(value) and not value.startswith(("-", "$", "[REDACTED:"))


def _is_flag_secret(value: str) -> bool:
    """A secret-named flag's value that is not a path, test id, call or small count."""
    if not _is_value(value) or looks_like_code_reference(value):
        return False
    return not (value.isascii() and value.isdigit() and len(value) <= _COUNT_MAX_DIGITS)


def is_secret_long_flag(name: str) -> bool:
    """Whether a ``--name`` flag takes a credential as its value (``--token``, ``--api-key``)."""
    n = name.lstrip("-").lower().replace("-", "_")
    if n.startswith(_BOOLEAN_FLAG_PREFIXES):
        return False
    return n in _SECRET_LONG_FLAGS or n.endswith(_SECRET_LONG_FLAG_SUFFIXES)


def _flag_value(words: list[Secret], i: int, flag: str, *, separate: bool) -> Secret | None:
    """The value of ``flag`` at ``words[i]``: attached (``-pX``, ``--flag=X``) or the next word."""
    word = words[i]
    if word.value == flag:
        return words[i + 1] if separate and i + 1 < len(words) else None
    lead = flag + "=" if flag.startswith("--") else flag
    return word.after(lead) if word.value.startswith(lead) and word.value != lead else None


def _curl_password(words: list[Secret], i: int) -> Secret | None:
    """The password after the first ``:`` of a curl ``-u user:password``."""
    for flag in _CURL_USER_FLAGS:
        pair = _flag_value(words, i, flag, separate=True)
        if pair is None:
            continue
        user, colon, password = pair.value.partition(":")
        if not colon or not password:
            return None
        if pair.prefix or not pair.raw.endswith(password):
            return Secret(pair.start, pair.end, password, pair.raw, f"{pair.prefix}{user}:")
        return Secret(pair.end - len(password), pair.end, password, password)
    return None


def _long_flag_value(words: list[Secret], i: int) -> Secret | None:
    word = words[i]
    if not word.value.startswith("--"):
        return None
    name = word.value.partition("=")[0]
    if not is_secret_long_flag(name):
        return None
    found = _flag_value(words, i, name, separate=True)
    return found if found is not None and _is_flag_secret(found.value) else None


def _flag_secrets(tool: str, words: list[Secret], *, login: bool) -> Iterator[Secret]:
    for i in range(len(words)):
        if tool in _MYSQL_TOOLS:
            found = _flag_value(words, i, "-p", separate=False)
        elif tool == "curl":
            found = _curl_password(words, i)
        elif tool == "sshpass" or login:
            found = _flag_value(words, i, "-p", separate=True)
        else:
            found = None
        if found is None and tool not in _MYSQL_TOOLS:
            found = _long_flag_value(words, i)
        if found is not None:
            yield found


def _htpasswd_password(words: list[Secret]) -> Secret | None:
    """The password of ``htpasswd -b [file] user password`` (``-n`` drops the file)."""
    flags = ""
    positionals: list[Secret] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word.value in _HTPASSWD_VALUE_FLAGS:
            skip = True
        elif word.value.startswith("-") and len(word.value) > 1:
            flags += word.value[1:]
        else:
            positionals.append(word)
    index = 1 if "n" in flags else 2
    if "b" not in flags or len(positionals) <= index:
        return None
    return positionals[index]


def _config_secret(words: list[Secret]) -> Secret | None:
    """The value of ``config set <key> <value>`` when the key names a credential."""
    values = [w for w in words if not w.value.startswith("-")]
    for i in range(len(values) - 3):
        verb, action, key, value = values[i : i + 4]
        if verb.value in _CONFIG_VERBS and action.value == "set" and "=" not in key.value:
            secret_key = is_secret_param(key.value, in_url=False) or key.value.endswith("_auth")
            return value if secret_key else None
    return None


def command_secrets(tool: str, args: str) -> list[Secret]:
    """The secrets in ``args``, the words after ``tool`` on its command line, by their spans."""
    words = _split_words(args)
    login = tool in _LOGIN_TOOLS and any(w.value == "login" for w in words)
    found: list[Secret | None] = []
    step = MAX_COMMAND_WORDS - WINDOW_OVERLAP
    for start in range(0, max(len(words) - WINDOW_OVERLAP, 1), step):
        window = words[start : start + MAX_COMMAND_WORDS]
        found.extend(_flag_secrets(tool, window, login=login))
    if tool == "htpasswd":
        found.append(_htpasswd_password(words))
    if tool in _CONFIG_TOOLS:
        found.append(_config_secret(words))
    unique = {s.start: s for s in found if s is not None and _is_value(s.value)}
    return sorted(unique.values(), key=lambda s: s.start)


def long_flag_secret(text: str, start: int, name: str, value: str) -> str | None:
    """The unquoted value of ``--name value`` at ``text[start]`` when it holds a credential.

    Outside a known tool's command the flag may be named in prose ("pass --token to it"), so the
    value needs a digit or symbol (or 20+ characters) and must not be a path or file name.
    ``--password`` takes no separate value for the MySQL client tools, so it is skipped when one
    of them runs earlier in the same shell command.
    """
    if not is_secret_long_flag(name):
        return None
    starts, mysql_ends = _mysql_runs(text)
    if mysql_ends[bisect_right(starts, start) - 1] <= start:
        return None
    unquoted = _unquote(value)
    probe = value.split()[0].translate(_QUOTES) if unquoted is None else unquoted
    if not _is_flag_secret(probe) or not looks_like_secret(probe):
        return None
    return value if unquoted is None else unquoted


def _segments(text: str) -> Iterator[tuple[int, int]]:
    pos = 0
    for m in _SEGMENT_START_RE.finditer(text):
        yield pos, m.start()
        pos = m.end()
    yield pos, len(text)


@lru_cache(maxsize=1)
def _mysql_runs(text: str) -> tuple[list[int], list[int]]:
    """Where each shell command in ``text`` starts, and where its first MySQL client word ends
    (past the end of ``text`` when it has none), read once for all of its flags."""
    starts: list[int] = []
    ends: list[int] = []
    for seg_start, seg_end in _segments(text):
        words = _split_words(text[seg_start:seg_end])
        mysql = next((w.end for w in words if w.value.rsplit("/", 1)[-1] in _MYSQL_TOOLS), None)
        starts.append(seg_start)
        ends.append(len(text) + 1 if mysql is None else seg_start + mysql)
    return starts, ends
