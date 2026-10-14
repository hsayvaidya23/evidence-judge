---
description: Development setup, conventions, and how to add checkers and evidence adapters to Evidence Judge.
---

# Contributing

<!-- ej:include CONTRIBUTING.md -->

## Working on these docs

The site is built with [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) from `docs/` and `mkdocs.yml`:

```bash
uv run --group docs mkdocs serve        # live preview at http://127.0.0.1:8000
uv run --group docs mkdocs build --strict
```

`--strict` turns every warning (a broken link, a missing anchor, a page left out of the nav) into an error, and the same strict build runs in CI and in `tests/unit/test_docs_site.py`.
