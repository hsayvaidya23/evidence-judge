"""The directories an MCP client may name files in, and the checks that keep it inside them."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


class PathNotAllowedError(ValueError):
    """A path the client named is missing, the wrong kind, or outside every allowed root."""


@dataclass(frozen=True, slots=True)
class Roots:
    """Allowed root directories, fully resolved. A path is inside a root when its fully resolved
    form (every symlink followed) is the root or below it, so a symlink cannot lead out."""

    dirs: tuple[Path, ...]

    @classmethod
    def of(cls, dirs: Iterable[str | Path]) -> Roots:
        resolved: list[Path] = []
        for d in dirs:
            path = Path(d).expanduser()
            try:
                path = path.resolve(strict=True)
            except OSError as e:
                raise PathNotAllowedError(f"root {d}: {e.strerror or e}") from e
            if not path.is_dir():
                raise PathNotAllowedError(f"root {d} is not a directory")
            if path not in resolved:
                resolved.append(path)
        if not resolved:
            raise PathNotAllowedError("at least one root directory is required")
        return cls(tuple(resolved))

    @property
    def default(self) -> Path:
        return self.dirs[0]

    def describe(self) -> str:
        return ", ".join(str(d) for d in self.dirs)

    def _absolute(self, value: str) -> Path:
        if not value.strip():
            raise PathNotAllowedError("empty path")
        if "\0" in value:
            raise PathNotAllowedError("path contains a NUL byte")
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.default / path

    def _contains(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.dirs)

    def _inside(self, path: Path, what: str, value: str) -> Path:
        if not self._contains(path):
            raise PathNotAllowedError(
                f"{what} {value!r} is outside the allowed roots ({self.describe()}); start the "
                "server with --root to allow another directory"
            )
        return path

    def existing(self, value: str, *, what: str = "path") -> Path:
        """An existing file or directory inside a root, resolved. Relative paths are taken
        against the first root."""
        path = self._absolute(value)
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as e:
            raise PathNotAllowedError(f"{what} {value!r} does not exist") from e
        except OSError as e:
            raise PathNotAllowedError(f"{what} {value!r}: {e.strerror or e}") from e
        return self._inside(resolved, what, value)

    def file(self, value: str, *, what: str = "file") -> Path:
        path = self.existing(value, what=what)
        if not path.is_file():
            raise PathNotAllowedError(f"{what} {value!r} is not a regular file")
        return path

    def directory(self, value: str, *, what: str = "directory") -> Path:
        path = self.existing(value, what=what)
        if not path.is_dir():
            raise PathNotAllowedError(f"{what} {value!r} is not a directory")
        return path

    def output(self, value: str, *, what: str = "output") -> Path:
        """A file to write inside a root. Its directory must exist; an existing target must be a
        regular file, never a symlink, so a write cannot be redirected outside the roots."""
        path = self._absolute(value)
        if path.name in ("", ".", ".."):
            raise PathNotAllowedError(f"{what} {value!r} is not a file name")
        parent = self.directory(str(path.parent), what=f"{what} directory")
        target = parent / path.name
        if target.is_symlink():
            raise PathNotAllowedError(
                f"{what} {value!r} is a symlink; refusing to write through it"
            )
        if target.exists() and not target.is_file():
            raise PathNotAllowedError(f"{what} {value!r} exists and is not a regular file")
        return self._inside(target, what, value)


def walk(top: Path, *, max_depth: int, skip: Sequence[str]) -> Iterable[tuple[Path, list[str]]]:
    """Directories under ``top`` (not following symlinks) with their file names, at most
    ``max_depth`` levels deep, skipping directories named in ``skip``."""
    base_depth = len(top.parts)
    for current, dirs, files in os.walk(top, followlinks=False):
        here = Path(current)
        if len(here.parts) - base_depth >= max_depth:
            dirs.clear()
        dirs[:] = sorted(d for d in dirs if d not in skip)
        yield here, files
