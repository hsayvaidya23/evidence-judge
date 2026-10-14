from __future__ import annotations

from evidence_judge.checks.matching import contains
from evidence_judge.textnorm import normalize


def test_typographic_punctuation_matches_ascii() -> None:
    assert normalize("Thank you — it’s “confirmed”") == 'thank you - it\'s "confirmed"'


def test_page_text_with_curly_quotes_matches_ascii_claim() -> None:
    assert contains("Order “1001” isn’t pending", "isn't pending")


def test_nbsp_and_case_are_folded() -> None:
    assert normalize("Red KETTLE  $24") == "red kettle $24"
