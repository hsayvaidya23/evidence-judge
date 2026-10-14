"""MkDocs hooks: include repository files and point links outside docs/ at the site or GitHub.

Docs stay readable on GitHub, where `../SECURITY.md` is a valid link. On the site, links that
leave `docs/` are rewritten: files the site renders as pages link to those pages, anything else
links to the file on GitHub. `<!-- ej:include FILE -->` inlines a repository file, resolving its
relative links against the repository root.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

REPO_BLOB = "https://github.com/hsayvaidya23/evidence-judge/blob/main/"
ROOT_PAGES = {
    "SECURITY.md": "security.md",
    "CONTRIBUTING.md": "contributing.md",
    "CHANGELOG.md": "changelog.md",
    "README.md": "index.md",
}
ANCHOR_PAGES = {"README.md#configuration": "reference/configuration.md"}
INCLUDE = re.compile(r"^<!-- ej:include (?P<file>\S+) -->$", re.M)
LINK = re.compile(r"(?<=\]\()(?P<target>[^)\s]+)(?=\))")
FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.M | re.S)
FIRST_HEADING = re.compile(r"\A# [^\n]*\n+")


def _rewrite(target: str, base: str, page_dir: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/", target, re.I):
        return target
    path, _, anchor = target.partition("#")
    resolved = posixpath.normpath(posixpath.join(base, path))
    if resolved.startswith("docs/"):
        inside = resolved.removeprefix("docs/")
        return _relative(inside, page_dir) + (f"#{anchor}" if anchor else "")
    key = f"{resolved}#{anchor}" if anchor else resolved
    if key in ANCHOR_PAGES:
        return _relative(ANCHOR_PAGES[key], page_dir)
    if resolved in ROOT_PAGES:
        return _relative(ROOT_PAGES[resolved], page_dir) + (f"#{anchor}" if anchor else "")
    return REPO_BLOB + resolved + (f"#{anchor}" if anchor else "")


def _relative(doc_path: str, page_dir: str) -> str:
    return posixpath.relpath(doc_path, page_dir or ".")


def _links(markdown: str, base: str, page_dir: str) -> str:
    def rewrite_text(text: str) -> str:
        return LINK.sub(lambda m: _rewrite(m.group("target"), base, page_dir), text)

    out: list[str] = []
    last = 0
    for fence in FENCE.finditer(markdown):
        out.append(rewrite_text(markdown[last : fence.start()]))
        out.append(fence.group(0))
        last = fence.end()
    out.append(rewrite_text(markdown[last:]))
    return "".join(out)


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    root = Path(config["config_file_path"]).parent
    page_dir = posixpath.dirname(page.file.src_uri)

    def include(match: re.Match[str]) -> str:
        name = match.group("file")
        text = (root / name).read_text(encoding="utf-8")
        text = FIRST_HEADING.sub("", text, count=1)
        return _links(text, posixpath.dirname(name), page_dir)

    markdown = _links(markdown, posixpath.join("docs", page_dir), page_dir)
    return INCLUDE.sub(include, markdown)
