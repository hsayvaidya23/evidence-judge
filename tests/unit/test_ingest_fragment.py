from __future__ import annotations

import pytest

from evidence_judge.ingest._fragment import fragment_text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("In <!-- zz --> stock", "In stock"),
        ("In<span hidden>zz</span> stock", "In stock"),
        ("In <b style='display: none'>zz</b>stock", "In stock"),
        ("<style>.x{display:none}</style>In <i class=x>zz</i>stock", "In stock"),
        ("In <span class=d-none>zz</span>stock", "In stock"),
        ("In <script>zz</script>stock", "In stock"),
        ("<template>zz</template>In stock", "In stock"),
        ("<p>In</p><p>stock</p>", "In stock"),
        ("In<br>stock", "In stock"),
        ("In&#32;stock &amp; more", "In stock & more"),
        ("<b>In <i>stock</b> now", "In stock now"),
        ("In</i> stock<img src=x/>", "In stock"),
        ("a < b", "a < b"),
    ],
    ids=[
        "comment",
        "hidden",
        "inline-style",
        "style-element",
        "utility-class",
        "script",
        "template",
        "blocks",
        "void",
        "references",
        "misnested",
        "stray-end-tag",
        "not-markup",
    ],
)
def test_fragment_text_is_what_the_page_shows(html: str, text: str) -> None:
    assert fragment_text(html) == text
