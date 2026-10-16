"""Comments that switch a linter or type checker off for a line or a file."""

from __future__ import annotations

import re
from collections import Counter
from typing import Final, Literal

_MARKERS: Final[tuple[tuple[str, Literal["lint", "typecheck"], re.Pattern[str]], ...]] = (
    ("# noqa", "lint", re.compile(r"#\s*(?:(?:ruff|flake8)\s*:\s*)?noqa\b", re.I)),
    ("# pylint: disable", "lint", re.compile(r"#\s*pylint\s*:\s*disable", re.I)),
    ("eslint-disable", "lint", re.compile(r"\b(?:eslint|oxlint)-disable\b")),
    ("biome-ignore", "lint", re.compile(r"\bbiome-ignore\b")),
    ("//nolint", "lint", re.compile(r"//\s*nolint\b")),
    ("#[allow(...)]", "lint", re.compile(r"#!?\[allow\(")),
    ("# rubocop:disable", "lint", re.compile(r"#\s*rubocop\s*:\s*disable")),
    ("# type: ignore", "typecheck", re.compile(r"#\s*type\s*:\s*ignore\b")),
    ("# pyright: ignore", "typecheck", re.compile(r"#\s*pyright\s*:\s*(?:ignore|basic)\b")),
    ("# mypy: ignore-errors", "typecheck", re.compile(r"#\s*mypy\s*:\s*(?:ignore-errors|disable)")),
    ("# pyre-ignore", "typecheck", re.compile(r"#\s*pyre-(?:ignore|fixme)\b")),
    ("@ts-ignore", "typecheck", re.compile(r"@ts-(?:ignore|expect-error|nocheck)\b")),
)
SUPPRESSES: Final = {label: category for label, category, _ in _MARKERS}
"""What each suppression comment hides: lint or type-check findings."""


def _count(text: str) -> Counter[str]:
    return Counter(
        {label: n for label, _, pattern in _MARKERS if (n := len(pattern.findall(text)))}
    )


def added_suppressions(
    removed: list[str] | None, added: list[str] | None, texts: tuple[str, str] | None = None
) -> list[str]:
    """The suppression comments an edit adds beyond those it removes (`# noqa`, `# type:
    ignore`, `eslint-disable`), by label; the whole files when the source records them."""
    before, after = (
        texts if texts is not None else ("\n".join(removed or []), "\n".join(added or []))
    )
    return sorted((_count(after) - _count(before)).keys())
