from __future__ import annotations

from pathlib import Path

import pytest

from evidence_judge.mcp.paths import PathNotAllowedError, Roots, walk


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "sub" / "a.txt").write_text("a", encoding="utf-8")
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    return root.resolve(), outside.resolve()


def test_roots_resolve_and_deduplicate(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    roots = Roots.of([root, root / "sub" / "..", root / "sub"])
    assert roots.dirs == (root, root / "sub")
    assert roots.default == root
    assert str(root) in roots.describe()


@pytest.mark.parametrize("bad", ["missing", "sub/a.txt"])
def test_roots_must_be_existing_directories(layout: tuple[Path, Path], bad: str) -> None:
    root, _ = layout
    with pytest.raises(PathNotAllowedError):
        Roots.of([root / bad])


def test_roots_need_one_directory() -> None:
    with pytest.raises(PathNotAllowedError, match="at least one"):
        Roots.of([])


def test_relative_paths_resolve_against_the_first_root(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    assert Roots.of([root]).file("sub/a.txt") == root / "sub" / "a.txt"


@pytest.mark.parametrize("value", ["../outside/secret.txt", "sub/../../outside/secret.txt"])
def test_dot_dot_cannot_leave_the_root(layout: tuple[Path, Path], value: str) -> None:
    root, _ = layout
    with pytest.raises(PathNotAllowedError, match="outside the allowed roots"):
        Roots.of([root]).file(value)


def test_absolute_paths_outside_are_refused(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    with pytest.raises(PathNotAllowedError, match="outside the allowed roots"):
        Roots.of([root]).file(str(outside / "secret.txt"))


def test_symlinks_out_of_the_root_are_refused(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    roots = Roots.of([root])
    with pytest.raises(PathNotAllowedError, match="outside"):
        roots.file("link.txt")
    with pytest.raises(PathNotAllowedError, match="outside"):
        roots.file("linkdir/secret.txt")


def test_symlinks_within_the_root_are_followed(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "inner.txt").symlink_to(root / "sub" / "a.txt")
    assert Roots.of([root]).file("inner.txt") == root / "sub" / "a.txt"


def test_a_second_root_allows_its_files(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    assert Roots.of([root, outside]).file(str(outside / "secret.txt")).name == "secret.txt"


@pytest.mark.parametrize(
    ("value", "match"),
    [("", "empty"), ("a\0b", "NUL"), ("nope.txt", "does not exist"), ("sub", "not a regular")],
)
def test_bad_file_values(layout: tuple[Path, Path], value: str, match: str) -> None:
    root, _ = layout
    with pytest.raises(PathNotAllowedError, match=match):
        Roots.of([root]).file(value)


def test_directory_must_be_a_directory(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    roots = Roots.of([root])
    assert roots.directory("sub") == root / "sub"
    with pytest.raises(PathNotAllowedError, match="not a directory"):
        roots.directory("sub/a.txt")


def test_output_paths(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    roots = Roots.of([root])
    assert roots.output("sub/new.json") == root / "sub" / "new.json"
    assert roots.output("sub/a.txt") == root / "sub" / "a.txt"
    (root / "out.json").symlink_to(outside / "secret.txt")
    with pytest.raises(PathNotAllowedError, match="symlink"):
        roots.output("out.json")
    with pytest.raises(PathNotAllowedError, match="outside"):
        roots.output(str(outside / "x.json"))
    with pytest.raises(PathNotAllowedError, match="does not exist"):
        roots.output("missing/x.json")
    with pytest.raises(PathNotAllowedError, match="not a regular file"):
        roots.output("sub")
    with pytest.raises(PathNotAllowedError, match="not a file name"):
        roots.output("sub/..")


def test_walk_stops_at_depth_and_skips(tmp_path: Path) -> None:
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    seen = [p.relative_to(tmp_path) for p, _ in walk(tmp_path, max_depth=2, skip=["node_modules"])]
    assert seen == [Path(), Path("a"), Path("a/b")]
