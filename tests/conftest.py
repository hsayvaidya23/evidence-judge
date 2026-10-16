from __future__ import annotations

import pytest

_COLOUR_FORCING = ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE", "TTY_COMPATIBLE")


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _COLOUR_FORCING:
        monkeypatch.delenv(name, raising=False)
