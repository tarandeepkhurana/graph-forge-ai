"""PDF layout cleanup: running headers, page numbers, reference lists.

    uv run python tests/test_layout.py

Every case here comes from a real failure on a research paper, where the
journal header became an entity in 14 of 98 triples and citations were
extracted as employment facts.
"""

from pathlib import Path

import pymupdf

from graphforge.ingest import layout

DATA = Path(__file__).resolve().parent / "data"


def _pdf(pages: list[list[str]], header: str | None = None, numbers: bool = False) -> pymupdf.Document:
    doc = pymupdf.open()
    for i, lines in enumerate(pages, 1):
        page = doc.new_page()
        if header:
            page.insert_text((50, 40), header, fontsize=8)
        y = 110
        for line in lines:
            page.insert_text((50, y), line, fontsize=11)
            y += 20
        if numbers:
            page.insert_text((300, 780), str(100 + i), fontsize=8)
    return doc


def test_running_header_removed() -> None:
    doc = _pdf(
        [[f"Body sentence number {i} about Neo4j."] for i in range(1, 6)],
        header="International Journal of Research, Vol 5",
    )
    text, report = layout.extract_clean_text(doc)
    doc.close()

    assert "International Journal" not in text, "running header survived"
    assert "Neo4j" in text, "body content was destroyed"
    assert report["lines_dropped"] >= 5


def test_page_numbers_removed() -> None:
    """Page numbers normalise to a one-character key -- the guard must not skip them."""
    doc = _pdf([[f"Sentence {i} about Cypher."] for i in range(1, 6)], numbers=True)
    text, report = layout.extract_clean_text(doc)
    doc.close()

    assert not any(str(100 + i) in text for i in range(1, 6)), "page numbers survived"
    assert "Cypher" in text


def test_short_documents_are_left_alone() -> None:
    """With one or two pages, 'repeats on most pages' is not evidence of anything."""
    doc = _pdf([["Neo4j is a graph database."], ["It uses Cypher."]],
               header="Some Header")
    text, _ = layout.extract_clean_text(doc)
    doc.close()

    # Too few pages to conclude anything -- better to keep noise than delete content.
    assert "Some Header" in text
    assert "Neo4j" in text


def test_body_text_is_not_mistaken_for_furniture() -> None:
    """A phrase repeated in the body must survive: only the page bands are furniture."""
    doc = _pdf([["Neo4j is a graph database.", "Neo4j is a graph database."]] * 5)
    text, _ = layout.extract_clean_text(doc)
    doc.close()
    assert "Neo4j is a graph database." in text


def test_references_section_removed() -> None:
    text, cut = layout.strip_references(
        "Intro paragraph.\n" * 20 + "REFERENCES\nAassal, A. El, Mehran University, 2020.\n"
    )
    assert cut is True
    assert "Aassal" not in text
    assert "Intro paragraph." in text


def test_reference_word_in_body_is_not_a_heading() -> None:
    """Papers discuss 'references' mid-text; truncating there would gut the document."""
    body = (
        "We consulted several references during this work.\n"
        + "More body text follows here.\n" * 20
    )
    text, cut = layout.strip_references(body)
    assert cut is False, "truncated on a body mention of 'references'"
    assert len(text) == len(body)


def test_numbered_reference_heading_matches() -> None:
    text, cut = layout.strip_references("Body.\n" * 20 + "6. Bibliography\nSmith, J., 2020.\n")
    assert cut is True
    assert "Smith" not in text


def test_end_to_end_on_paper_like_pdf() -> None:
    """The fixture reproduces the real failure: header + page numbers + references."""
    path = DATA / "paper_like.pdf"
    if not path.exists():
        return
    doc = pymupdf.open(path)
    text, report = layout.extract_clean_text(doc)
    doc.close()

    assert "International Journal" not in text
    assert "Aassal" not in text
    assert not any(str(n) in text for n in (866, 867, 868, 869, 870))
    for kept in ("Neo4j", "FastAPI", "Kubernetes", "Cypher", "METHODS", "CONCLUSION"):
        assert kept in text, f"{kept} was removed"
    assert report["references_removed"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall layout tests passed")
