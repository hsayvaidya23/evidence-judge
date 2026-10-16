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
_RAISED_DIGITS = {
    **{ord(c): f"^{d}" for d, c in enumerate("⁰¹²³⁴⁵⁶⁷⁸⁹")},
    **{ord(c): f"_{d}" for d, c in enumerate("₀₁₂₃₄₅₆₇₈₉")},
}
_RAISED = re.compile("[\u00b2\u00b3\u00b9\u2070\u2074-\u2079\u2080-\u2089]")
_PUNCT_PAIRS = tuple((chr(code), ascii_) for code, ascii_ in PUNCT_MAP.items())


def _ascii_punctuation(text: str) -> str:
    for char, ascii_ in _PUNCT_PAIRS:
        if char in text:
            text = text.replace(char, ascii_)
    return text


def normalize(text: str) -> str:
    """Drop default-ignorable code points, mark superscript and subscript digits ('2⁴' becomes
    '2^4', not the 24 NFKC makes of it), NFKC fold, ASCII punctuation, case-fold and collapse
    whitespace."""
    visible = _IGNORABLE.sub("", text)
    if _RAISED.search(visible):
        visible = visible.translate(_RAISED_DIGITS)
    folded = _ascii_punctuation(unicodedata.normalize("NFKC", visible)).casefold()
    return _WS.sub(" ", folded).strip()


_ZERO_DECIMALS = re.compile(r"[.,]00?(?!\d)")
_DIGIT_GROUP_JOINS = " '"
"""Thousands separators normalisation leaves between digit groups: a space (also a no-break,
thin or narrow no-break space) and an apostrophe ('1 100', "1'100")."""
_THREE_DIGITS = re.compile(r"\d{3}(?!\d)")
_SHORT_GROUP = re.compile(r"(?<!\d)\d{1,3}$")
_DIGITS = "0123456789"
MIN_ID_DIGITS = 3
"""Digits at the end of a phrase that make it an ID a letter or a dash and a letter can extend
('1001A', 'A-1001'); a shorter run ('10:00am', '2x') stands alone before a letter."""


def find_phrase(text: str, phrase: str) -> int:
    """Where normalised `phrase` first occurs in normalised `text`, or -1.

    A phrase holding a digit (an order number, a price, a reference) occurs only as a whole
    token: a digit at either end has no letter or digit beside it, nor a '-' joining it to one
    ('1001' is not in '1001A', 'A1001' or '1001-A'), a cased letter at either end has no cased
    letter or digit beside it, and no decimal or group separator joins it to more digits: '100'
    is not in '1001', '1,100', '1 100' or "1'100", and '$24' is not in '$24.50'. Trailing zero
    decimals are the same amount, so '$24' occurs in '$24.00'. Letters without case (Chinese,
    Japanese) mark no word boundary, so at an end that is one the phrase matches as a substring.
    Any other phrase is a plain substring.
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


def _touches(inner: str, outer: str, *, run: int) -> bool:
    """Whether `outer` continues the token whose end is `inner`. A letter continues a digit
    run of an ID's length (MIN_ID_DIGITS): '1001A', but not the '10:00' of '10:00am'."""
    if inner.isdigit():
        return outer.isdigit() or (outer.isalpha() and run >= MIN_ID_DIGITS)
    return _cased(inner) and (outer.isdigit() or _cased(outer))


def _stands_alone(text: str, start: int, end: int) -> bool:
    first, last = text[start], text[end - 1]
    before, after = text[max(0, start - 2) : start], text[end : end + 2]
    lead = len(text[start:end]) - len(text[start:end].lstrip(_DIGITS))
    trail = len(text[start:end]) - len(text[start:end].rstrip(_DIGITS))
    if start > 0 and _touches(first, text[start - 1], run=lead):
        return False
    if end < len(text) and _touches(last, text[end], run=trail):
        return False
    if lead >= MIN_ID_DIGITS and len(before) == 2 and before[1] == "-" and before[0].isalnum():
        return False
    if trail >= MIN_ID_DIGITS and after[:1] == "-" and after[1:].isalnum():
        return False
    if first.isdigit() and len(before) == 2 and before[0].isdigit():
        if before[1] in ".,":
            return False
        if _grouped(text, start - 1, start):
            return False
    if last.isdigit() and len(after) == 2 and after[1].isdigit():
        if after[0] in ".,":
            return _ZERO_DECIMALS.match(text, end) is not None
        if _grouped(text, end, end + 1):
            return False
    return True


def _grouped(text: str, separator: int, group: int) -> bool:
    """Whether the space or apostrophe at `separator` joins two digit groups: one to three
    digits before it, exactly three after it."""
    return (
        text[separator] in _DIGIT_GROUP_JOINS
        and _THREE_DIGITS.match(text, group) is not None
        and _SHORT_GROUP.search(text, 0, separator) is not None
    )
