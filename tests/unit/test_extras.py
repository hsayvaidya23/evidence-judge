from __future__ import annotations

import pytest

from evidence_judge._extras import needs_extra


@pytest.mark.parametrize(
    ("extra", "extras"),
    [("otel", "anthropic,mcp,otel"), ("mcp", "anthropic,mcp"), ("anthropic", "anthropic,mcp")],
)
def test_the_uv_hint_keeps_the_extras_already_installed(extra: str, extras: str) -> None:
    hint = needs_extra(extra)
    assert f"reinstall with {extra} added to the extras you already use" in hint
    assert f'uv tool install --python 3.12 --reinstall "evidence-judge[{extras}]"' in hint


def test_the_pip_hint_installs_from_pypi() -> None:
    assert 'pip install "evidence-judge[langfuse]"' in needs_extra("langfuse")
