"""Committed trace fixtures carry no paths from the machine that recorded them."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from scripts.fixture_hygiene import scrub_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TRACES = sorted(FIXTURES.rglob("*.zip"))
LOCAL = re.compile(r"/Users/|/home/[a-z]|/var/folders/|C:\\\\Users\\\\")


def test_there_are_trace_fixtures() -> None:
    assert len(TRACES) > 20


@pytest.mark.parametrize("trace", TRACES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_trace_has_no_stacks_or_local_paths(trace: Path) -> None:
    with zipfile.ZipFile(trace) as z:
        names = z.namelist()
        assert not [n for n in names if n.endswith(".stacks")]
        for name in names:
            if name.endswith((".trace", ".network")):
                text = z.read(name).decode("utf-8")
                assert not LOCAL.search(text), (name, LOCAL.search(text))


def test_scrub_keeps_file_names_and_drops_machine_paths() -> None:
    text = '{"localPaths":["/private/var/folders/ym/abc123/T/tmpq1w2/invoice.txt"]}'
    assert scrub_text(text) == '{"localPaths":["/tmp/tmpq1w2/invoice.txt"]}'
