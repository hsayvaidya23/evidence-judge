"""Choosing the files the re-run copies, and copying them."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

MAX_FILES: Final = 200_000


MAX_BYTES: Final = 2 * 1024**3


_DEPENDENCY_DIRS: Final = (".venv", "venv", "node_modules")


_SKIPPED_DIRS: Final = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
     ".hypothesis", ".evidence-judge", *_DEPENDENCY_DIRS}
)  # fmt: skip


def git(
    root: Path, *args: str, env: dict[str, str], data: bytes = b""
) -> subprocess.CompletedProcess[bytes] | None:
    git = shutil.which("git", path=env["PATH"])
    if git is None:
        return None
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "-c", "core.fsmonitor=false", "-c", f"core.excludesFile={os.devnull}",
             "-C", str(root), *args],
            env=env | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
            input=data,
            capture_output=True,
            timeout=60,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None


_PER_DIRECTORY: Final = "--exclude-per-directory=.gitignore"


def _paths(data: bytes) -> list[str]:
    return [p for p in data.decode("utf-8", "surrogateescape").split("\0") if p]


def _git_files(root: Path, env: dict[str, str]) -> list[str] | None:
    """Tracked files, and untracked files the tree's `.gitignore` files do not ignore, under
    `root`; None outside a git tree. The personal exclude files (`.git/info/exclude`,
    `core.excludesFile`) are not read: they are not part of the project, and anything running
    as the user can write them."""
    done = git(root, "ls-files", "-z", "--cached", "--others", _PER_DIRECTORY, env=env)
    if done is None or done.returncode != 0:
        return None
    return sorted(set(_paths(done.stdout)))


def _work_tree_holds(root: Path, env: dict[str, str]) -> bool:
    """The work tree git reports contains `root`; `core.worktree` can point git at another
    directory, whose untracked files would then decide what is copied."""
    done = git(root, "rev-parse", "--show-toplevel", env=env)
    if done is None or done.returncode != 0:
        return False
    try:
        top = Path(done.stdout.decode("utf-8", "surrogateescape").strip()).resolve()
    except OSError:
        return False
    return root.is_relative_to(top)


@dataclass(frozen=True, slots=True)
class Base:
    """The revision the working tree is compared with, and the files tracked in it under the
    project directory (relative to it)."""

    sha: str
    files: tuple[str, ...]


def resolve_base(root: Path, revision: str, env: dict[str, str]) -> Base | None:
    resolved = git(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", env=env)
    if resolved is None or resolved.returncode != 0:
        return None
    sha = resolved.stdout.decode().strip()
    listing = git(root, "ls-tree", "-r", "-z", "--name-only", sha, "--", ".", env=env)
    if listing is None or listing.returncode != 0:
        return None
    return Base(sha, tuple(_paths(listing.stdout)))


def show(root: Path, sha: str, path: str, env: dict[str, str]) -> bytes | None:
    shown = git(root, "show", f"{sha}:{path}", env=env)
    return shown.stdout if shown is not None and shown.returncode == 0 else None


def _lines(data: bytes | None) -> set[str]:
    if data is None:
        return set()
    return {line.strip() for line in data.decode("utf-8", "replace").splitlines()}


def newly_ignored(root: Path, base: Base, env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Untracked paths only `.gitignore` rules added since `base` ignore, which the copy takes
    anyway; and exceptions (`!pattern`) removed since `base`, whose effect cannot be undone
    that way."""
    listed = git(root, "ls-files", "-z", "--others", "--ignored", _PER_DIRECTORY, "--directory",
                  env=env)  # fmt: skip
    hidden: list[str] = []
    if listed is not None and listed.returncode == 0 and listed.stdout:
        matched = git(root, "check-ignore", "-z", "-v", "--no-index", "--stdin", env=env,
                       data=listed.stdout)  # fmt: skip
        fields = _paths(matched.stdout) if matched is not None else []
        rules: dict[str, set[str]] = {}
        for source, _, pattern, path in zip(*[iter(fields)] * 4, strict=False):
            if PurePosixPath(source).name != ".gitignore":
                hidden.append(path)
                continue
            if source not in rules:
                rules[source] = _lines(show(root, base.sha, source, env))
            if pattern.strip() not in rules[source]:
                hidden.append(path)
    dropped: list[str] = []
    for path in base.files:
        if PurePosixPath(path).name != ".gitignore":
            continue
        try:
            now = _lines((root / path).read_bytes())
        except OSError:
            now = set[str]()
        before = _lines(show(root, base.sha, f"./{path}", env))
        dropped.extend(
            f"{path} no longer has the exception {line}"
            for line in sorted(before - now)
            if line.startswith("!")
        )
    return hidden, dropped


class CopyError(Exception):
    pass


@dataclass(slots=True)
class _Budget:
    files: int = 0
    size: int = 0

    def take(self, path: Path) -> None:
        self.files += 1
        with contextlib.suppress(OSError):
            self.size += path.lstat().st_size
        if self.files > MAX_FILES or self.size > MAX_BYTES:
            raise CopyError(
                f"the working tree is larger than the re-run copies ({MAX_FILES} files, "
                f"{MAX_BYTES // 1024**3} GiB)"
            )


def _copy_one(src: Path, dst: Path, budget: _Budget) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        budget.take(src)
        dst.symlink_to(src.readlink())
    elif src.is_file():
        budget.take(src)
        shutil.copy2(src, dst)
    elif src.is_dir():
        _copy_tree(src, dst, budget)


def _copy_tree(src: Path, dst: Path, budget: _Budget) -> None:
    for here, dirs, files in os.walk(src, followlinks=False):
        base = Path(here)
        target = dst / base.relative_to(src)
        target.mkdir(parents=True, exist_ok=True)
        keep: list[str] = []
        for name in sorted(dirs):
            if name in _SKIPPED_DIRS:
                continue
            if (base / name).is_symlink():
                _copy_one(base / name, target / name, budget)
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            _copy_one(base / name, target / name, budget)


@dataclass(slots=True)
class _Copied:
    """What the copy holds and how it was chosen, with what the choice needs said about it."""

    paths: list[str]
    how: str
    notes: list[str] = field(default_factory=list[str])
    problems: list[str] = field(default_factory=list[str])
    config_changes: list[str] = field(default_factory=list[str])


def _files_under(dest: Path, rel: str) -> list[str]:
    top = dest / rel
    if top.is_symlink() or not top.is_dir():
        return [rel] if top.is_symlink() or top.exists() else []
    return [p.relative_to(dest).as_posix() for p in top.rglob("*") if not p.is_dir()]


def _skipped(rel: str) -> bool:
    return any(part in _SKIPPED_DIRS for part in PurePosixPath(rel).parts)


def _included(root: Path, patterns: list[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for match in root.glob(pattern):
            rel = match.relative_to(root).as_posix()
            if not _skipped(rel):
                found.add(rel)
    return sorted(found)


def copy_tree(
    root: Path,
    dest: Path,
    env: dict[str, str],
    base: Base | None = None,
    include: list[str] | None = None,
) -> _Copied:
    """Copy the working tree into `dest`. In a git tree: every tracked file still present and
    every untracked file the project's `.gitignore` files do not ignore (so the agent's new
    files come along, caches and build output do not), plus what `base` tracked and what
    `.gitignore` rules added since `base` hide, so changing the index or the ignore rules
    cannot keep a file out of the run. Elsewhere: everything but VCS metadata, caches and
    dependency directories. Paths matching `include` are copied either way. Dependency
    directories (`.venv`, `venv`, `node_modules`) are linked, not copied."""
    budget = _Budget()
    listed = _git_files(root, env)
    if listed is not None and not _work_tree_holds(root, env):
        listed = None
        problems = [
            "the git configuration points the work tree away from the project, so the whole "
            "directory was copied instead of the files git lists"
        ]
    else:
        problems = []
    if listed is None:
        how = "the whole directory" + ("" if problems else " (not a git tree)")
        _copy_tree(root, dest, budget)
        result = _Copied(_files_under(dest, "."), how, problems=problems)
    else:
        result = _Copied([], "git ls-files (tracked, and untracked not ignored by .gitignore)")
        wanted = set(listed)
        if base is not None:
            wanted.update(base.files)
            hidden, result.config_changes = newly_ignored(root, base, env)
            hidden = [h.rstrip("/") for h in hidden if not _skipped(h.rstrip("/"))]
            if hidden:
                result.notes.append(
                    f"copied {len(hidden)} path(s) that .gitignore rules added since "
                    f"{base.sha[:12]} ignore: {', '.join(hidden[:3])}"
                )
            wanted.update(hidden)
        for rel in sorted(wanted):
            src = root / rel
            if (src.is_symlink() or src.exists()) and not (dest / rel).exists():
                _copy_one(src, dest / rel, budget)
                result.paths.extend(_files_under(dest, rel))
    extra = _included(root, include or [])
    for rel in extra:
        if not (dest / rel).exists() and not (dest / rel).is_symlink():
            _copy_one(root / rel, dest / rel, budget)
            result.paths.extend(_files_under(dest, rel))
    if extra:
        result.notes.append(f"copied {len(extra)} path(s) the spec's rerun.include names")
    for name in _DEPENDENCY_DIRS:
        source = root / name
        if source.is_dir() and not (dest / name).exists() and not (dest / name).is_symlink():
            (dest / name).symlink_to(source, target_is_directory=True)
    result.paths = sorted(set(result.paths))
    return result
