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


_ZERO_DECIMALS = re.compile(r"[.,]00?(?!\d)")


def find_phrase(text: str, phrase: str) -> int:
    """Where normalised `phrase` first occurs in normalised `text`, or -1.

    A phrase holding a digit (an order number, a price, a reference) occurs only as a whole
    token: a digit at either end has no digit beside it, a cased letter at either end has no
    cased letter or digit beside it, and no decimal or group separator joining it to more digits, so '100' does not occur
    in '1001' or '1,100' and '$24' does not occur in '$24.50'. Trailing zero decimals are the
    same amount, so '$24' occurs in '$24.00'. Letters without case (Chinese, Japanese) mark no
    word boundary, so at an end that is one the phrase matches as a substring. Any other phrase is
    a plain substring.
    """
    at = text.find(phrase)
    if at < 0 or not any(c.isdigit() for c in phrase):
        return at
    while at >= 0:
        if _stands_alone(text, at, at + len(phrase)):
            return at
        at = text.find(phrase, at + 1)
    return -1


def _cased(char: str) -> bool:
    return char.upper() != char.lower()


def _touches(inner: str, outer: str) -> bool:
    if inner.isdigit():
        return outer.isdigit()
    return _cased(inner) and (outer.isdigit() or _cased(outer))


def _stands_alone(text: str, start: int, end: int) -> bool:
    if start > 0 and _touches(text[start], text[start - 1]):
        return False
    if end < len(text) and _touches(text[end - 1], text[end]):
        return False
    if (
        text[start].isdigit()
        and start >= 2
        and text[start - 1] in ".,"
        and text[start - 2].isdigit()
    ):
        return False
    if (
        text[end - 1].isdigit()
        and end + 1 < len(text)
        and text[end] in ".,"
        and text[end + 1].isdigit()
    ):
        return _ZERO_DECIMALS.match(text, end) is not None
    return True
