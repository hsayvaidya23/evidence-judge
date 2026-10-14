from __future__ import annotations

import logging

import pytest

from evidence_judge.llm import pricing
from evidence_judge.llm.pricing import DEFAULT_PRICE, DEFAULT_PRICES, is_known_model, price_for


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_warned_unknown", set[str]())


def test_known_model_uses_its_price_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=pricing.__name__):
        assert price_for("claude-haiku-5-5") is DEFAULT_PRICES["claude-haiku-5-5"]
    assert is_known_model("claude-haiku-5-5")
    assert caplog.records == []


def test_unknown_model_warns_once_and_uses_fail_safe_price(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=pricing.__name__):
        assert price_for("claude-future-9") is DEFAULT_PRICE
        assert price_for("claude-future-9") is DEFAULT_PRICE
        price_for("claude-other-1")
    assert not is_known_model("claude-future-9")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "claude-future-9" in messages[0]
    assert "claude-other-1" in messages[1]


def test_custom_table_decides_what_is_known() -> None:
    table = {"my-model": DEFAULT_PRICE}
    assert is_known_model("my-model", table)
    assert not is_known_model("claude-haiku-5-5", table)
