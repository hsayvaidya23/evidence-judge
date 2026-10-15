"""Runner configuration: changes from the base revision and files above the copy."""

from __future__ import annotations

import configparser
import json
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Final, cast

from evidence_judge.sessions.rerun.copy import Base, show
from evidence_judge.sessions.rerun.spec import Runner

_CONFIG_NAMES: Final = frozenset(
    {"pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
     "pyproject.toml", "conftest.py", "sitecustomize.py", "usercustomize.py", "package.json",
     ".npmrc", ".yarnrc", ".yarnrc.yml", "go.mod", "go.work", "Cargo.toml", ".mocharc.json",
     ".mocharc.yml", ".mocharc.yaml", ".mocharc.js", ".mocharc.cjs", "noxfile.py"}
)  # fmt: skip


_CONFIG_PREFIXES: Final = ("jest.config.", "vitest.config.", "vitest.workspace.", "vite.config.")


def is_runner_config(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.name in _CONFIG_NAMES or pure.name.startswith(_CONFIG_PREFIXES):
        return True
    return pure.parent.name == ".cargo" and pure.name in ("config", "config.toml")


_PYTEST_ABOVE: Final = frozenset(
    {"pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
     "pyproject.toml", "conftest.py"}
)  # fmt: skip


_NODE_ABOVE: Final = frozenset({"package.json", "node_modules", ".babelrc", ".babelrc.json"})


_ABOVE: Final[dict[Runner, Callable[[str], bool]]] = {
    "pytest": lambda name: name in _PYTEST_ABOVE,
    "unittest": lambda _: False,
    "go": lambda name: name in ("go.mod", "go.work"),
    "cargo": lambda name: name in ("Cargo.toml", ".cargo"),
    "node": lambda name: name in _NODE_ABOVE,
    "jest": lambda name: name in _NODE_ABOVE or name.startswith(("jest.config.", "babel.config.")),
    "vitest": lambda name: (
        name in _NODE_ABOVE
        or name.startswith(("vitest.config.", "vitest.workspace.", "vite.config."))
    ),
}


def configuration_above(copy: Path, runner: Runner, env: dict[str, str]) -> list[str]:
    """Files in the directories above the copy that the runner would read as the project's
    configuration (pytest looks upwards for an ini file and conftest.py, go for go.work, node
    for node_modules). The copy lives in the system temporary directory, which anything running
    as the user can write to."""
    wanted = _ABOVE[runner]
    cargo_home = Path(env["CARGO_HOME"]) if "CARGO_HOME" in env else None
    found: list[str] = []
    for parent in copy.parents:
        try:
            names = sorted(p.name for p in parent.iterdir())
        except OSError:
            continue
        for name in names:
            if wanted(name) and parent / name != cargo_home:
                found.append(str(parent / name))
    return found


def _section(path: str, data: bytes | None) -> tuple[str, object]:
    """The part of a configuration file that configures the tests, and its label."""
    if data is None:
        return "", None
    name = PurePosixPath(path).name
    try:
        if name == "pyproject.toml":
            tool: object = tomllib.loads(data.decode("utf-8")).get("tool")
            settings = (
                cast(dict[str, object], tool).get("pytest") if isinstance(tool, dict) else None
            )
            return " [tool.pytest]", settings
        if name == "setup.cfg":
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(data.decode("utf-8"))
            section = parser["tool:pytest"] if parser.has_section("tool:pytest") else None
            return " [tool:pytest]", dict(section) if section is not None else None
        if name == "package.json":
            loaded: object = json.loads(data)
            if isinstance(loaded, dict):
                keys = ("scripts", "jest", "mocha", "ava", "type")
                package = cast(dict[str, object], loaded)
                return " (scripts and test settings)", {k: package.get(k) for k in keys}
    except (ValueError, UnicodeDecodeError, configparser.Error):
        pass
    return "", data


def config_changes(
    root: Path, copy: Path, copied: list[str], base: Base, env: dict[str, str]
) -> list[str]:
    """Runner configuration that differs between the copy and the base revision."""
    before = {p for p in base.files if is_runner_config(p)}
    now = {p for p in copied if is_runner_config(p) and (copy / p).is_file()}
    changes: list[str] = []
    for path in sorted(before | now):
        old = show(root, base.sha, f"./{path}", env) if path in before else None
        new = (copy / path).read_bytes() if path in now else None
        label, old_part = _section(path, old)
        new_label, new_part = _section(path, new)
        if old_part == new_part:
            continue
        if old is None:
            changes.append(f"{path} was added")
        elif new is None:
            changes.append(f"{path} was deleted")
        else:
            changes.append(f"{path}{label or new_label} changed")
    return changes
