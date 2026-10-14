from __future__ import annotations

import re
import unicodedata

import pytest

from evidence_judge.checks.matching import contains
from evidence_judge.textnorm import PUNCT_MAP, normalize

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


def _reference_normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(PUNCT_MAP).casefold()
    return re.sub(r"\s+", " ", folded).strip()


@pytest.mark.parametrize("code", sorted(PUNCT_MAP))
def test_each_mapped_code_point_matches_translate(code: int) -> None:
    char = chr(code)
    for text in (char, f"a{char}b", f"{char}{char} X {char}"):
        assert normalize(text) == _reference_normalize(text)


def test_mixed_text_matches_translate() -> None:
    mixed = "".join(chr(c) for c in sorted(PUNCT_MAP))
    accents = "Caf\N{LATIN SMALL LETTER E WITH ACUTE} Stra\N{LATIN SMALL LETTER SHARP S}e"
    wide = "\N{FULLWIDTH DIGIT ONE}\N{FULLWIDTH DIGIT TWO}"
    text = f"  {accents} {mixed} {wide} {LDQUO}ok{RDQUO}\t\n{EM_DASH}x{NBSP} " * 50
    assert normalize(text) == _reference_normalize(text)
