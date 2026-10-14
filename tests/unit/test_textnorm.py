from __future__ import annotations

from evidence_judge.checks.matching import contains
from evidence_judge.textnorm import normalize

EM_DASH = "\N{EM DASH}"
LSQUO, RSQUO = "\N{LEFT SINGLE QUOTATION MARK}", "\N{RIGHT SINGLE QUOTATION MARK}"
LDQUO, RDQUO = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
NBSP = "\N{NO-BREAK SPACE}"


def test_typographic_punctuation_matches_ascii() -> None:
    text = f"Thank you {EM_DASH} it{RSQUO}s {LDQUO}confirmed{RDQUO}"
    assert normalize(text) == 'thank you - it\'s "confirmed"'


def test_page_text_with_curly_quotes_matches_ascii_claim() -> None:
    assert contains(f"Order {LDQUO}1001{RDQUO} isn{RSQUO}t pending", "isn't pending")
    assert contains(f"{LSQUO}quoted{RSQUO}", "'quoted'")


def test_nbsp_and_case_are_folded() -> None:
    assert normalize(f"Red{NBSP}KETTLE  $24") == "red kettle $24"
