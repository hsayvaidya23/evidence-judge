"""Text normalisation shared by claim extraction and evidence matching."""

from __future__ import annotations

import re
import unicodedata

_SINGLE_QUOTES = (0x2018, 0x2019, 0x201A, 0x201B)
_DOUBLE_QUOTES = (0x201C, 0x201D, 0x201E)
_DASHES = (0x2013, 0x2014, 0x2212)
PUNCT_MAP: dict[int, str] = {
    **dict.fromkeys(_SINGLE_QUOTES, "'"),
    **dict.fromkeys(_DOUBLE_QUOTES, '"'),
    **dict.fromkeys(_DASHES, "-"),
    0x00A0: " ",
}
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """NFKC fold, ASCII punctuation, case-fold and collapse whitespace."""
    folded = unicodedata.normalize("NFKC", text).translate(PUNCT_MAP).casefold()
    return _WS.sub(" ", folded).strip()
