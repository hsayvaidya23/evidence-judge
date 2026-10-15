"""Keep recorded fixtures free of the recording machine's paths.

A Playwright trace carries ``trace.stacks`` (the source files of every call) and absolute paths
in error stacks and upload paths. None of it is evidence the judge reads, but it names the
machine's home and checkout directories. ``scrub_trace`` drops the stacks and replaces those
paths with placeholders, keeping file names so uploads still read as ``invoice.txt``.

    uv run python -m scripts.fixture_hygiene tests/fixtures/**/*.zip
"""

from __future__ import annotations

import argparse
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
_TEXT: Final = (".trace", ".network")
_TEMP: Final = re.compile(r"(?:/private)?/var/folders/[^\"'\s)]*?/T/|/tmp/")


def _local_prefixes() -> list[tuple[str, str]]:
    temp = str(Path(tempfile.gettempdir()).resolve())
    return [(str(ROOT), "/repo"), (temp, "/tmp"), (str(Path.home()), "/home")]


def scrub_text(text: str) -> str:
    for prefix, placeholder in _local_prefixes():
        text = text.replace(prefix, placeholder)
    return _TEMP.sub("/tmp/", text)


def scrub_trace(path: Path) -> bool:
    """Rewrite the trace zip at `path` without local paths; True if anything changed."""
    with zipfile.ZipFile(path) as zin:
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]
    kept: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        if info.filename.endswith(".stacks"):
            continue
        if info.filename.endswith(_TEXT):
            data = scrub_text(data.decode("utf-8")).encode("utf-8")
        kept.append((info, data))
    if [(i.filename, d) for i, d in kept] == [(i.filename, d) for i, d in entries]:
        return False
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in kept:
            zout.writestr(info, data)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove local paths from trace fixtures.")
    parser.add_argument("traces", nargs="+", type=Path)
    args = parser.parse_args()
    for trace in args.traces:
        if scrub_trace(trace):
            print(f"scrubbed {trace}")


if __name__ == "__main__":
    main()
