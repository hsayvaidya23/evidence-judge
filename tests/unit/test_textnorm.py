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
