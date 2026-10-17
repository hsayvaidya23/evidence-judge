"""Credentials passed on a command line: which argument of which tool holds a secret.

A command is split into shell words with their offsets, each word unquoted with ``shlex`` (or
kept as written when its quoting is unbalanced), then matched against per-tool flag tables.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass

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
_WORD = r"""(?:'[^'\n]*'|"(?:[^"\\\n]|\\.)*"|\\.|[^\s'"\\|;&<>`()])+"""
_WORD_RE = re.compile(_WORD)
_TOOL_NAMES = "|".join(sorted(map(re.escape, _TOOLS), key=len, reverse=True))
COMMAND_RE = re.compile(rf"(?<![\w.\\=:@-])({_TOOL_NAMES})(?![\w./-])((?:[ \t]+{_WORD})+)")
"""A known tool's name and the words after it, up to the end of its shell command."""
LONG_FLAG_RE = re.compile(rf"(?<![\w-])(--[A-Za-z][\w-]{{0,63}})([ \t]+)({_WORD})")
"""``--name value`` on any command line, for flags named like a credential."""
_SEGMENT_START_RE = re.compile(r"[\n;|&`(]")


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


def _split_words(text: str) -> list[Secret]:
    words: list[Secret] = []
    for m in _WORD_RE.finditer(text):
        raw = m.group(0)
        try:
            value = "".join(shlex.split(raw))
        except ValueError:
            value = raw
        words.append(Secret(m.start(), m.end(), value, raw))
    return words


def _is_value(value: str) -> bool:
    """A flag's value that is itself the secret, not another flag or a variable reference."""
    return bool(value) and not value.startswith(("-", "$", "[REDACTED:"))


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
    return _flag_value(words, i, name, separate=True) if is_secret_long_flag(name) else None


def _flag_secrets(tool: str, words: list[Secret]) -> Iterator[Secret]:
    login = tool in _LOGIN_TOOLS and any(w.value == "login" for w in words)
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
    found: list[Secret | None] = list(_flag_secrets(tool, words))
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
    segment = _SEGMENT_START_RE.split(text[:start])[-1]
    if any(w.value.rsplit("/", 1)[-1] in _MYSQL_TOOLS for w in _split_words(segment)):
        return None
    words = _split_words(value)
    unquoted = words[0].value if words else value
    if not _is_value(unquoted) or looks_like_code_reference(unquoted):
        return None
    return unquoted if looks_like_secret(unquoted) else None
