"""Strip page furniture and reference lists from PDF text.

Two failure modes, both observed on a real research paper:

**Running headers and footers.** A journal header repeated on every page landed
*mid-sentence* in the extracted text, and the extraction model dutifully turned
it into an entity. On a 5-page paper that was 14 of 98 triples -- roughly one
edge in seven was noise from a line the reader never even looks at.

**The bibliography.** Citations were extracted as facts:
`Aassal, A. El -[works_for]-> Mehran University`. Nobody works for a journal
because they published in it. A reference list is dense with names and
organisations, so it produces a lot of confident nonsense.

The proper tool for this is a layout-aware parser (Docling, GROBID). Both are
out of reach here: docling-serve wants ~8 GB of resident RAM, and no free
hosting tier offers more than 512 MB, so it would have to run on the same
laptop as a 2.7 GB extraction model. This module does the same two jobs with
the position data PyMuPDF already hands us.

The method for headers is the standard one: furniture is text that repeats at
the same place on many pages, so count normalised lines by page band and drop
the frequent ones. It is what layout parsers do internally, minus the ML.
"""

from __future__ import annotations

import re
from collections import Counter

# Fraction of page height counted as the header band and the footer band.
_BAND = 0.12

# A line must appear in the same band on at least this share of pages to be
# considered furniture. Two-thirds is deliberately strict: a phrase repeated on
# most pages of a document is almost never content, but a *body* line can
# legitimately recur a few times.
_REPEAT_RATIO = 0.6

# Below this many pages, "repeats on most pages" is not evidence of anything.
_MIN_PAGES = 3

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")

# Headings that start a reference list. Matched on a line of its own, allowing
# a leading number ("6. REFERENCES") and trailing punctuation.
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:\d+[.)]?\s*)?"
    r"(references?|bibliography|works\s+cited|literature\s+cited)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)


def _norm(line: str) -> str:
    """Normalise for counting: page numbers vary, the rest of the header does not."""
    return _DIGITS.sub("#", _WS.sub(" ", line).strip().casefold())


def find_furniture(pages: list[list[tuple[str, float]]]) -> set[str]:
    """Return normalised lines that behave like running headers or footers.

    `pages` is one list per page of `(line_text, relative_y)` where relative_y
    is 0.0 at the top of the page and 1.0 at the bottom.
    """
    if len(pages) < _MIN_PAGES:
        return set()

    # Count per band, not overall: a phrase in the header on page 1 and in the
    # body on page 4 is not furniture, and counting them together would hide that.
    header = Counter()
    footer = Counter()

    for page in pages:
        seen_top, seen_bottom = set(), set()
        for text, y in page:
            key = _norm(text)
            # Length is checked on the raw text, not the key. `_norm` collapses
            # digits to "#", so a page number normalises to a one-character key
            # -- guarding on key length would skip the single most common piece
            # of furniture there is.
            if not key or not text.strip():
                continue
            if y <= _BAND:
                seen_top.add(key)
            elif y >= 1.0 - _BAND:
                seen_bottom.add(key)
        # Count once per page, so a line repeated twice on one page is not
        # mistaken for a line appearing on two pages.
        header.update(seen_top)
        footer.update(seen_bottom)

    threshold = max(_MIN_PAGES - 1, int(len(pages) * _REPEAT_RATIO))
    return {
        key
        for counter in (header, footer)
        for key, count in counter.items()
        if count >= threshold
    }


def strip_references(text: str) -> tuple[str, bool]:
    """Cut everything from a `References` heading onwards.

    Only acts on a heading in the last 40% of the document. Papers discuss
    "references" in their body text, and truncating at the first mention would
    throw away most of the document.
    """
    lines = text.splitlines(keepends=True)
    floor = int(len(lines) * 0.6)

    offset = sum(len(line) for line in lines[:floor])
    for index in range(floor, len(lines)):
        if _REFERENCE_HEADING.match(lines[index]):
            return text[:offset], True
        offset += len(lines[index])

    return text, False


def extract_clean_text(doc) -> tuple[str, dict]:
    """Text for a PyMuPDF document, with furniture and references removed.

    Returns the text plus a small report, so ingestion can log what it dropped
    rather than silently discarding parts of the user's document.
    """
    page_lines: list[list[tuple[str, float]]] = []

    for page in doc:
        height = page.rect.height or 1.0
        lines: list[tuple[str, float]] = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line.get("spans", []))
                if text.strip():
                    # y midpoint of the line, as a fraction of page height
                    top, bottom = line["bbox"][1], line["bbox"][3]
                    lines.append((text, ((top + bottom) / 2) / height))
        page_lines.append(lines)

    furniture = find_furniture(page_lines)

    kept_pages: list[str] = []
    dropped = 0
    for lines in page_lines:
        kept = []
        for text, y in lines:
            in_band = y <= _BAND or y >= 1.0 - _BAND
            if in_band and _norm(text) in furniture:
                dropped += 1
                continue
            kept.append(text)
        kept_pages.append("\n".join(kept))

    # Form feeds keep page boundaries so chunk page numbers stay accurate.
    text = "\f".join(kept_pages)
    text, cut_references = strip_references(text)

    return text, {
        "furniture_patterns": len(furniture),
        "lines_dropped": dropped,
        "references_removed": cut_references,
    }
