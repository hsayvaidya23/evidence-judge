"""The hooks' private state, kept outside the workspace: the task spec as it was when the turn
started, so an agent that edits the spec cannot weaken the check of its own answer; and the
evidence-log key and head anchor, so a rewritten or truncated log shows."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

DEFAULT_STATE_DIR: Final = Path.home() / ".evidence-judge" / "state"
_SESSION_ID: Final = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")
BROWSER_SPEC: Final = "spec"
SESSION_SPEC: Final = "session-spec"
_MAX_SPEC_BYTES: Final = 1024 * 1024


def resolve_state_dir(option: str | None) -> Path:
    """``--state-dir`` when it is a usable absolute path (an unexpanded ``${...}`` is not), else
    ``EVJ_HOOK_STATE_DIR``, else ``$CLAUDE_PLUGIN_DATA/state``, else ``~/.evidence-judge/state``."""
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    for value in (
        option,
        os.environ.get("EVJ_HOOK_STATE_DIR"),
        str(Path(plugin_data) / "state") if plugin_data else None,
    ):
        if value and "${" not in value and Path(value).is_absolute():
            return Path(value)
    return DEFAULT_STATE_DIR


def session_dir(base: Path, session_id: str) -> Path:
    """The session's state directory; an unusual id is replaced by its hash, so it cannot
    escape ``base``."""
    if _SESSION_ID.match(session_id) and session_id not in (".", ".."):
        return base / session_id
    return base / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (mode 0600) through a temporary file and an atomic rename."""
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)


def digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def read_spec(path: Path | None) -> bytes | None:
    """The spec's bytes, or None when there is none or it is too large to be a spec."""
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_SPEC_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > _MAX_SPEC_BYTES else data


@dataclass(frozen=True, slots=True)
class SpecSnapshot:
    source: str | None
    """The spec file the turn started with, or None when there was none."""
    sha256: str | None
    copy: Path | None
    """The private copy to judge with."""
    taken_at: float
    git_head: str | None = None
    """The project's git revision when the turn started, for comparing runner configuration."""


def snapshot_spec(
    base: Path,
    session_id: str,
    spec: Path | None,
    *,
    kind: str = BROWSER_SPEC,
    git_head: str | None = None,
) -> SpecSnapshot:
    """Save the spec of this ``kind`` the turn starts with (or that there is none) in the
    session's state."""
    directory = session_dir(base, session_id)
    data = read_spec(spec)
    copy: Path | None = None
    if data is not None and spec is not None:
        copy = directory / f"{kind}-snapshot{spec.suffix}"
        write_private(copy, data)
    snapshot = SpecSnapshot(
        source=str(spec) if data is not None else None,
        sha256=digest(data),
        copy=copy,
        taken_at=time.time(),
        git_head=git_head,
    )
    meta = {
        "source": snapshot.source,
        "sha256": snapshot.sha256,
        "copy": str(copy) if copy else None,
        "taken_at": snapshot.taken_at,
        "git_head": git_head,
    }
    write_private(directory / f"{kind}-snapshot.json", json.dumps(meta).encode("utf-8"))
    return snapshot


def load_snapshot(base: Path, session_id: str, kind: str = BROWSER_SPEC) -> SpecSnapshot | None:
    """The session's spec snapshot of this ``kind``, or None when no turn-start hook saved one.
    A snapshot whose copy no longer matches its digest matches no spec."""
    try:
        raw: object = json.loads(
            (session_dir(base, session_id) / f"{kind}-snapshot.json").read_bytes()
        )
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    meta = cast(dict[str, object], raw)
    source, sha, copy, taken, head = (
        meta.get(k) for k in ("source", "sha256", "copy", "taken_at", "git_head")
    )
    if not isinstance(taken, int | float):
        return None
    copy_path = Path(copy) if isinstance(copy, str) else None
    if copy_path is not None and digest(read_spec(copy_path)) != sha:
        return SpecSnapshot("(a damaged snapshot)", "damaged", None, float(taken))
    return SpecSnapshot(
        source=source if isinstance(source, str) else None,
        sha256=sha if isinstance(sha, str) else None,
        copy=copy_path,
        taken_at=float(taken),
        git_head=head if isinstance(head, str) else None,
    )


def environment_path(base: Path, session_id: str) -> Path:
    """Where the turn-start hook keeps the fingerprint of the project's dependency directories
    (see ``sessions.fingerprint``) for the stop hook's re-run."""
    return session_dir(base, session_id) / f"{SESSION_SPEC}-environment.json"


def save_environment(base: Path, session_id: str, fingerprint: dict[str, object]) -> Path:
    path = environment_path(base, session_id)
    write_private(path, json.dumps(fingerprint).encode("utf-8"))
    return path


def drop_environment(base: Path, session_id: str) -> None:
    """Forget the fingerprint, so a turn without one is never compared with an older turn's."""
    with contextlib.suppress(OSError):
        environment_path(base, session_id).unlink()


LOG_CHAIN: Final = "hmac-sha256"
LOG_KEY_FILE: Final = "log-key"
LOG_HEAD_FILE: Final = "log-head.json"
_KEY_BYTES: Final = 32


def read_log_key(path: Path) -> bytes | None:
    try:
        key = bytes.fromhex(path.read_text("ascii").strip())
    except (OSError, ValueError):
        return None
    return key if len(key) == _KEY_BYTES else None


def log_key(base: Path) -> bytes:
    """The installation's evidence-log key (``<base>/log-key``, mode 0600), created on first
    use. Anyone who can read it can sign a forged log: keep ``base`` out of the agent's reach.

    Raises:
        ValueError: the key file exists but holds no usable key.
    """
    path = base / LOG_KEY_FILE
    key = read_log_key(path)
    if key is not None:
        return key
    _private_dir(base)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = read_log_key(path)
        if key is None:
            raise ValueError(f"{path} is not a usable evidence-log key") from None
        return key
    key = os.urandom(_KEY_BYTES)
    try:
        os.write(fd, key.hex().encode("ascii") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


def key_id(key: bytes) -> str:
    """A public fingerprint of the key, so a log names the key it was signed with."""
    return hashlib.sha256(b"evidence-judge log key id\0" + key).hexdigest()[:16]


def line_mac(key: bytes, line: bytes) -> str:
    return hmac.new(key, line, hashlib.sha256).hexdigest()


def log_head_path(base: Path, session_id: str) -> Path:
    return session_dir(base, session_id) / LOG_HEAD_FILE


def write_log_head(base: Path, session_id: str, count: int, head: str) -> Path:
    """Anchor the log: how many lines it has and the MAC of its last line, kept apart from the
    log so that cutting lines off its end shows."""
    path = log_head_path(base, session_id)
    write_private(path, json.dumps({"count": count, "head": head}).encode("utf-8"))
    return path


def read_log_head(path: Path) -> tuple[int, str] | None:
    try:
        raw: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    count, head = data.get("count"), data.get("head")
    if isinstance(count, int) and not isinstance(count, bool) and isinstance(head, str):
        return count, head
    return None
