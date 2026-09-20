"""Ingestion tests.

Run with:  uv run python tests/test_ingest.py

The offset test is the important one. Grounding (feature 4) shows the user the
source text by slicing the original document with a chunk's stored offsets, so if
those drift, the side panel quietly shows the wrong passage. An earlier version of
the chunker passed on Markdown and drifted on PDFs, which is why both are covered.
"""

from pathlib import Path

from graphforge.extraction.schema import Triple, clean_triples
from graphforge.ingest.chunking import chunk_text, load_text

DATA = Path(__file__).resolve().parent / "data"


def test_chunk_offsets_reconstruct_source() -> None:
    for path in sorted(DATA.glob("*")):
        if path.suffix.lower() not in {".md", ".txt", ".pdf"}:
            continue
        text = load_text(path)
        chunks = chunk_text(text, source=path.name)
        assert chunks, f"{path.name} produced no chunks"
        for c in chunks:
            assert text[c.char_start : c.char_end] == c.text, f"offset drift in {c.chunk_id}"


def test_chunks_overlap() -> None:
    """Overlap keeps a fact straddling a boundary whole in at least one chunk."""
    text = load_text(DATA / "sample.md")
    chunks = chunk_text(text, source="sample.md", chunk_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start < a.char_end, "consecutive chunks must overlap"


def test_no_scrap_chunks() -> None:
    """Structural splitting must not leave tiny fragments that each cost a model pass."""
    text = load_text(DATA / "sample.md")
    chunks = chunk_text(text, source="sample.md", chunk_tokens=100, overlap_tokens=20)
    tiny = [len(c.text) for c in chunks[1:] if len(c.text) < 100]
    assert not tiny, f"scrap chunks survived: {tiny}"


def test_cleanup_fixes_observed_failure_modes() -> None:
    """Every case here was seen in a real gliner-relex run on a research paper."""
    cleaned = clean_triples([
        Triple(subject="Nobel Prize in\n1903", predicate="is_a", object="Award"),
        Triple(subject="Neo4j", predicate="uses", object="Cypher", confidence=0.52),
        Triple(subject="Neo4j", predicate="uses", object="Cypher", confidence=0.94),
        Triple(subject="The extraction pipeline", predicate="measures", object="precision"),
        Triple(subject="The benchmark", predicate="is_a", object="benchmark"),
    ])
    by_subject = {t.subject: t for t in cleaned}

    assert "Nobel Prize in 1903" in by_subject, "newline inside entity span not collapsed"
    assert "extraction pipeline" in by_subject, "leading article not stripped"
    assert len([t for t in cleaned if t.object == "Cypher"]) == 1, "duplicate edge kept"
    assert by_subject["Neo4j"].confidence == 0.94, "dedup kept the weaker reading"
    assert all(t.subject != "benchmark" for t in cleaned), "self-loop not dropped"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall ingest tests passed")
