"""Document loading and chunking.

Written here rather than in the app because Phase 0 needs it, and it moves into
`app/src/graphforge/ingest/` unchanged in Phase 1. Chunk boundaries follow
structure (headings, then paragraphs) so a chunk is a coherent unit of meaning --
extraction quality depends on this more than on any model setting.

**Chunks are offset ranges, never rebuilt strings.** A chunk stores (start, end)
and its text is always `source_text[start:end]`, so `text[c.char_start:c.char_end]
== c.text` holds by construction. An earlier version joined segments with "\\n\\n"
and computed the end from the joined length; that silently drifted on PDFs, whose
paragraph separators are " \\n \\n" and similar. Grounding (feature 4) depends on
these offsets pointing at the real span, so the invariant is load-bearing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# Approximation used when tiktoken is not worth loading: ~4 chars per token.
CHARS_PER_TOKEN = 4

DEFAULT_CHUNK_TOKENS = 1000
DEFAULT_OVERLAP_TOKENS = 150

# Spans shorter than this get merged into their neighbour. Without it, structural
# splitting leaves 100-400 char scraps that cost a full model pass each and carry
# almost no extractable content.
MIN_CHUNK_CHARS = 500

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".rst", ".py", ".js", ".ts"}


@dataclass
class Chunk:
    chunk_id: str
    text: str
    source: str
    ord: int
    char_start: int
    char_end: int
    page: int | None = None
    meta: dict = field(default_factory=dict)


def load_text(path: Path) -> str:
    """Extract plain text from one file. Page breaks become form feeds."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        import pymupdf  # imported lazily; heavy and only needed for PDFs

        with pymupdf.open(path) as doc:
            return "\f".join(page.get_text() for page in doc)

    if suffix == ".docx":
        import docx

        return "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs)

    return path.read_text(encoding="utf-8", errors="replace")


# Split on markdown/rst headings first, then blank lines.
_HEADING = re.compile(r"^(#{1,6} .*|.+\n[=-]{3,})$", re.MULTILINE)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def _segment_spans(text: str) -> list[tuple[int, int]]:
    """Structural boundaries as (start, end) offsets into `text`.

    Offsets only -- never substrings -- so nothing can drift.
    """
    cuts = sorted({0, len(text), *(m.start() for m in _HEADING.finditer(text))})

    spans: list[tuple[int, int]] = []
    for block_start, block_end in zip(cuts, cuts[1:]):
        pos = block_start
        for m in _BLANK_LINE.finditer(text, block_start, block_end):
            if text[pos : m.start()].strip():
                spans.append((pos, m.start()))
            pos = m.end()
        if text[pos:block_end].strip():
            spans.append((pos, block_end))
    return spans


def _merge_small(spans: list[tuple[int, int]], max_chars: int) -> list[tuple[int, int]]:
    """Fold scraps into the preceding span while the budget allows."""
    out: list[tuple[int, int]] = []
    for start, end in spans:
        if out and (end - start) < MIN_CHUNK_CHARS and end - out[-1][0] <= max_chars:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def chunk_text(
    text: str,
    source: str = "",
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Pack structural segments up to the token budget, with leading overlap."""
    max_chars = chunk_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    # 1. Group segments into (start, end) ranges that fit the budget.
    ranges: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None

    for start, end in _segment_spans(text):
        if end - start > max_chars:
            # One oversized segment (a huge table, a minified line): hard-split it.
            if cur:
                ranges.append(cur)
                cur = None
            pos = start
            while pos < end:
                stop = min(pos + max_chars, end)
                ranges.append((pos, stop))
                pos = stop
            continue

        if cur is None:
            cur = (start, end)
        elif end - cur[0] <= max_chars:
            cur = (cur[0], end)
        else:
            ranges.append(cur)
            cur = (start, end)

    if cur:
        ranges.append(cur)

    ranges = _merge_small(ranges, max_chars)

    # 2. Materialise. Overlap is applied by moving char_start backwards, so the
    #    slice stays exact and a fact split across a boundary is whole in one chunk.
    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(ranges):
        char_start = max(ranges[i - 1][0] + 1, start - overlap_chars) if i else start
        body = text[char_start:end]
        if not body.strip():
            continue
        digest = hashlib.sha1(f"{source}:{char_start}:{end}".encode()).hexdigest()[:12]
        chunks.append(
            Chunk(
                chunk_id=f"{Path(source).stem or 'chunk'}-{len(chunks):04d}-{digest}",
                text=body,
                source=source,
                ord=len(chunks),
                char_start=char_start,
                char_end=end,
                page=text.count("\f", 0, char_start) + 1 if "\f" in text else None,
            )
        )
    return chunks


def load_chunks(path: Path, **kw) -> list[Chunk]:
    return chunk_text(load_text(path), source=path.name, **kw)


def load_corpus(directory: Path, **kw) -> list[Chunk]:
    """Chunk every supported file in a directory tree."""
    out: list[Chunk] = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
            out.extend(load_chunks(p, **kw))
    return out
