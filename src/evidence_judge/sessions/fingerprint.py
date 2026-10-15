"""A fingerprint of the dependency directories the re-run links into its copy instead of copying
(`.venv`, `venv`, `node_modules`), so a change made to them during the session shows.

The re-run's copy links these directories back to the user's tree, so the runner, its plugins
and every installed package come from there. The turn-start hook saves a fingerprint, the re-run
takes another, and any difference means the suite ran against an environment the session
changed. A virtualenv is fingerprinted file by file (size, modification and status-change time,
which no unprivileged process can set back); `node_modules` by the name, version and file status
of each top-level package's manifest and of its `.bin` entries, which is cheap but does not see
an edit inside a package.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Final, cast

FORMAT: Final = 1
_PYTHON_DIRS: Final = (".venv", "venv")
_MAX_FILES: Final = 300_000
_SKIPPED: Final = frozenset({"__pycache__"})
_HOOK_FILES: Final = frozenset({"sitecustomize.py", "usercustomize.py"})

type Fingerprint = dict[str, object]


def _status(path: str, stat: os.stat_result) -> str:
    return f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ctime_ns}\n"


def _python_env(top: Path) -> Fingerprint:
    digest = hashlib.sha256()
    packages: list[str] = []
    hooks: dict[str, str] = {}
    files = 0
    stack = [top]
    while stack:
        here = stack.pop()
        try:
            entries = sorted(os.scandir(here), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            rel = Path(entry.path).relative_to(top).as_posix()
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _SKIPPED:
                        continue
                    if entry.name.endswith((".dist-info", ".egg-info")):
                        packages.append(entry.name)
                    digest.update(f"{rel}/\n".encode("utf-8", "surrogateescape"))
                    stack.append(Path(entry.path))
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            files += 1
            if files > _MAX_FILES:
                return {"too_large": True}
            digest.update(_status(rel, stat).encode("utf-8", "surrogateescape"))
            if entry.name.endswith(".pth") or entry.name in _HOOK_FILES:
                try:
                    hooks[rel] = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
                except OSError:
                    hooks[rel] = "unreadable"
    return {
        "digest": digest.hexdigest(),
        "files": files,
        "packages": sorted(packages),
        "hooks": hooks,
    }


def _manifests(modules: Path) -> list[Path]:
    out: list[Path] = []
    try:
        entries = sorted(os.scandir(modules), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith("@"):
            try:
                out.extend(
                    Path(e.path) for e in sorted(os.scandir(entry.path), key=lambda e: e.name)
                )
            except OSError:
                continue
        elif not entry.name.startswith("."):
            out.append(Path(entry.path))
    return out


def _node_modules(top: Path) -> Fingerprint:
    digest = hashlib.sha256()
    packages: list[str] = []
    for package in _manifests(top):
        manifest = package / "package.json"
        try:
            data = manifest.read_bytes()
            stat = manifest.stat()
        except OSError:
            continue
        try:
            loaded: object = json.loads(data)
        except ValueError:
            loaded = None
        version = (
            cast(dict[str, object], loaded).get("version") if isinstance(loaded, dict) else None
        )
        rel = package.relative_to(top).as_posix()
        packages.append(f"{rel}@{version}")
        digest.update(_status(f"{rel}/package.json", stat).encode("utf-8", "surrogateescape"))
    bins = top / ".bin"
    try:
        entries = sorted(os.scandir(bins), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        try:
            target = str(Path(entry.path).readlink()) if entry.is_symlink() else ""
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        digest.update(
            _status(f".bin/{entry.name}->{target}", stat).encode("utf-8", "surrogateescape")
        )
    return {"digest": digest.hexdigest(), "packages": packages}


def fingerprint(root: Path) -> Fingerprint:
    """The fingerprint of `root`'s dependency directories (those that exist)."""
    dirs: dict[str, Fingerprint] = {}
    for name in _PYTHON_DIRS:
        path = root / name
        if path.is_dir():
            dirs[name] = _python_env(path.resolve())
    modules = root / "node_modules"
    if modules.is_dir():
        dirs["node_modules"] = _node_modules(modules.resolve())
    return {"format": FORMAT, "dirs": dirs}


def _listed(entry: Fingerprint, key: str) -> list[str]:
    value = entry.get(key)
    return [str(v) for v in cast(list[object], value)] if isinstance(value, list) else []


def _hooks(entry: Fingerprint) -> dict[str, str]:
    value = entry.get("hooks")
    return (
        {str(k): str(v) for k, v in cast(dict[object, object], value).items()}
        if isinstance(value, dict)
        else {}
    )


def _describe(name: str, before: Fingerprint, after: Fingerprint) -> str | None:
    if before.get("too_large") or after.get("too_large"):
        return f"{name} has too many files to fingerprint, so a change to it cannot be ruled out"
    if before.get("digest") == after.get("digest") and before.get("digest") is not None:
        return None
    old, new = set(_listed(before, "packages")), set(_listed(after, "packages"))
    parts: list[str] = []
    if added := sorted(new - old):
        parts.append(f"added {', '.join(added[:3])}" + (" ..." if len(added) > 3 else ""))
    if removed := sorted(old - new):
        parts.append(f"removed {', '.join(removed[:3])}" + (" ..." if len(removed) > 3 else ""))
    old_hooks, new_hooks = _hooks(before), _hooks(after)
    if hooks := sorted(
        k for k in old_hooks.keys() | new_hooks.keys() if old_hooks.get(k) != new_hooks.get(k)
    ):
        parts.append(f"changed {', '.join(hooks[:3])}")
    return f"{name}: {'; '.join(parts) if parts else 'files changed in place'}"


def changes(baseline: Fingerprint, current: Fingerprint) -> list[str]:
    """How the dependency directories differ from `baseline`, one line per directory."""
    if baseline.get("format") != FORMAT:
        return ["the environment fingerprint from the start of the turn is unreadable"]
    before = cast(dict[str, object], baseline.get("dirs") or {})
    after = cast(dict[str, object], current.get("dirs") or {})
    out: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old is None:
            out.append(f"{name} was created")
        elif new is None:
            out.append(f"{name} was removed")
        elif not isinstance(old, dict) or not isinstance(new, dict):
            out.append(f"{name}: the fingerprint is unreadable")
        elif line := _describe(name, cast(Fingerprint, old), cast(Fingerprint, new)):
            out.append(line)
    return out


def load(path: Path) -> Fingerprint | None:
    """A saved fingerprint, or None when the file is missing or not one."""
    try:
        raw: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return cast(Fingerprint, raw) if isinstance(raw, dict) else None
