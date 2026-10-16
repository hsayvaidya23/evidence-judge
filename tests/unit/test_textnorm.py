from __future__ import annotations

import re
import shutil
import unicodedata
import zipfile
from pathlib import Path

import pytest

from evidence_judge.bench.dataset import load_scenario
from evidence_judge.checks.matching import contains
from evidence_judge.config import Settings
from evidence_judge.core.verdict import Outcome
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.textnorm import PUNCT_MAP, find_phrase, normalize

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


@pytest.mark.parametrize(
    "char",
    [
        "\N{SOFT HYPHEN}",
        "\N{ZERO WIDTH SPACE}",
        "\N{ZERO WIDTH NON-JOINER}",
        "\N{ZERO WIDTH JOINER}",
        "\N{WORD JOINER}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
        "\N{LEFT-TO-RIGHT MARK}",
        "\N{VARIATION SELECTOR-16}",
        "\N{TAG LATIN SMALL LETTER A}",
    ],
)
def test_default_ignorable_code_points_are_dropped(char: str) -> None:
    assert normalize(f"Out of st{char}ock") == "out of stock"
    assert contains(f"<p>Out of st{char}ock</p>", "Out of stock")
    assert contains("Out of stock", f"out of st{char}ock")


def test_dropping_ignorables_keeps_visible_text() -> None:
    assert normalize("a\N{SOFT HYPHEN} b") == "a b"
    assert normalize("\N{ZERO WIDTH SPACE}") == ""


async def test_soft_hyphenated_page_text_passes_the_honest_read(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "runs" / "read_stock"
    run_dir = shutil.copytree(source, tmp_path / "read_stock")
    old, new = '"Out of stock"]', '"Out of st\\u00adock"]'
    with (
        zipfile.ZipFile(source / "trace.zip") as zin,
        zipfile.ZipFile(run_dir / "trace.zip", "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename.endswith(".trace"):
                text = data.decode()
                assert old in text
                data = text.replace(old, new).encode()
            zout.writestr(info, data)
    scenario = load_scenario(run_dir)
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    verdict = await judge.judge(scenario.run, JudgeOptions(offline=True, spec=scenario.spec))
    assert verdict.outcome is Outcome.PASS, verdict.summary


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("order #1001", "1001"),
        ("/orders/1001", "1001"),
        ('{"order_id": 1001}', "1001"),
        ("total: $24.00", "$24"),
        ("total: $24.00", "$24.00"),
        ("total 24,00 eur", "24"),
        ("at 10:00am", "10:00"),
        ("2x kettle 24kg", "24"),
        ("ref ord-1001", "ord-1001"),
        ("order #1001-", "1001"),
        ("total 1 100 €", "1 100 €"),
        ("total 1'100", "1'100"),
        ("2^4 = 16", "2^4"),
        ("account acc-2041.", "acc-2041"),
        ("out of stocks", "out of stock"),
        ("reconfirmed", "confirm"),
    ],
)
def test_phrase_found(text: str, phrase: str) -> None:
    assert find_phrase(text, phrase) >= 0


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("/orders/1001", "100"),
        ("/orders/1001", "001"),
        ("order #21001", "1001"),
        ("1,100 items", "100"),
        ("price 1.100", "100"),
        ("total: $24.50", "$24"),
        ("total: $24.001", "$24.00"),
        ("24.000 eur", "24"),
        ("acc-20411", "acc-2041"),
        ("xacc-2041", "acc-2041"),
        ("ref ord1001", "1001"),
        ("order 1001a", "1001"),
        ("order a1001", "1001"),
        ("order 1001-a", "1001"),
        ("order #1001-a", "#1001"),
        ("order hd-1001", "1001"),
        ("total: 1 100 €", "100 €"),
        ("total 1 100", "100"),
        ("total 1'100", "100"),
        ("100 000 items", "100"),
    ],
)
def test_number_inside_a_longer_token_is_not_found(text: str, phrase: str) -> None:
    assert find_phrase(text, phrase) == -1


def test_a_later_whole_occurrence_is_found() -> None:
    assert find_phrase("orders 1001 and 100", "100") == 16
    assert contains("Orders 1001 and 100", "100")
    assert not contains("Order 1001", "100")


@pytest.mark.parametrize("space", ["\N{NARROW NO-BREAK SPACE}", "\N{THIN SPACE}", NBSP, "'"])
def test_a_thousands_separator_joins_digit_groups(space: str) -> None:
    assert not contains(f"Total 1{space}100", "100")
    assert contains(f"Total 1{space}100", f"1{space}100")


def test_superscript_digits_do_not_fold_into_the_number() -> None:
    assert normalize("2\N{SUPERSCRIPT FOUR}") == "2^4"
    assert normalize("H\N{SUBSCRIPT TWO}O") == "h_2o"
    assert not contains("area 2\N{SUPERSCRIPT FOUR} m", "24")
    assert contains("$24\N{SUPERSCRIPT ONE} incl. VAT", "$24")
