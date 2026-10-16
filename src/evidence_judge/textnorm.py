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
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_PUNCT_PAIRS = tuple((chr(code), ascii_) for code, ascii_ in PUNCT_MAP.items())


def _ascii_punctuation(text: str) -> str:
    for char, ascii_ in _PUNCT_PAIRS:
        if char in text:
            text = text.replace(char, ascii_)
    return text


def normalize(text: str) -> str:
    """Drop default-ignorable code points, NFKC fold, ASCII punctuation, case-fold and collapse
    whitespace."""
    visible = _IGNORABLE.sub("", text)
    folded = _ascii_punctuation(unicodedata.normalize("NFKC", visible)).casefold()
    return _WS.sub(" ", folded).strip()
