"""The ingestion pipeline: bytes in, graph out.

    parse -> chunk -> extract (per chunk) -> write -> embed -> resolve

Runs in a background worker, never in a request. Measured basis: the extraction
model takes ~2 s per chunk on CPU, so a 300-page PDF is roughly 20 minutes. A
user watching a spinner for 20 minutes is a broken product; a progress bar on a
job row is a working one.

**Why `resolve` is a separate stage.** The model is stateless per chunk -- it has
no memory of chunk 6 while reading chunk 7 -- so it emits the same real-world
thing under several surface forms. `write` folds together only what is already
byte-identical after normalising; everything else (`Cybercrime` / `CYBER CRIME`,
`IPv6` / `IPv6 protocol`) needs a pass that compares entities to each other.
That is `graph/resolution.py`, and in practice it is the stage that decides
whether the graph is usable. Extraction is the cheap part.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.core.config import get_settings
from graphforge.db.models import Document, DocumentStatus, Job, JobStatus
from graphforge.extraction import base as extractors, conceptmap
from graphforge.graph import queries, resolution, vectors
from graphforge.ingest import parsers
from graphforge.ingest.chunking import Chunk, chunk_text

log = logging.getLogger(__name__)

# How many chunks extract at once. Four keeps a document moving without
# tripping API rate limits or opening a socket per chunk on a big upload.
EXTRACTION_CONCURRENCY = 4


def _cfg():
    return get_settings()


# One shared model instance: loading weights takes ~20 s and 2.7 GB, so it is
# loaded once on first use and reused for every job.
_extractor: extractors.Extractor | None = None
_extractor_lock = asyncio.Lock()


async def get_extractor() -> extractors.Extractor:
    global _extractor
    async with _extractor_lock:
        if _extractor is None:
            log.info("loading extraction model %s", _cfg().extraction_model)
            instance = extractors.get(_cfg().extraction_model)
            # load() is blocking and slow; keep it off the event loop.
            await asyncio.to_thread(instance.load)
            _extractor = instance
        return _extractor


@dataclass
class IngestResult:
    chunks: int = 0
    triples: int = 0
    entities: int = 0
    merged: int = 0
    errors: list[str] = field(default_factory=list)


async def ingest_document(
    db: AsyncSession,
    document_id: uuid.UUID,
    data: bytes,
) -> IngestResult:
    """Ingest raw uploaded bytes: parse them, then run the rest of the pipeline."""
    document, job = await _load(db, document_id)
    await _set_status(db, document, DocumentStatus.parsing, job, progress=5)

    parsed = await asyncio.to_thread(parsers.parse, data, document.filename)
    document.pages = parsed.pages
    document.mime = parsed.mime

    return await _ingest_text(db, document, job, parsed.text)


async def _load(db: AsyncSession, document_id: uuid.UUID):
    document = await db.get(Document, document_id)
    if document is None:
        raise LookupError(f"document {document_id} not found")
    job = (
        await db.execute(select(Job).where(Job.document_id == document_id))
    ).scalar_one_or_none()
    return document, job


async def _ingest_text(
    db: AsyncSession, document: Document, job: Job | None, text: str
) -> IngestResult:
    """Everything after parsing: chunk -> extract -> write -> resolve.

    Split out because not every source needs parsing. A repository arrives as
    text already -- sending it back through `parsers.parse` made it look like an
    unknown file type and failed the whole job.
    """
    result = IngestResult()
    try:
        chunks = chunk_text(
            text,
            source=document.filename,
            chunk_tokens=_cfg().limits.chunk_tokens,
            overlap_tokens=_cfg().limits.chunk_overlap_tokens,
        )
        result.chunks = len(chunks)
        if not chunks:
            raise ValueError("no readable text found in this document")

        await queries.upsert_document(
            document.workspace_id, document.id, document.filename
        )
        await queries.write_chunks(
            document.workspace_id, document.id, [_chunk_row(c) for c in chunks]
        )

        await _set_status(db, document, DocumentStatus.extracting, job, progress=15)

        # One map for the whole document. Not a performance choice -- it is the
        # only way the model can tell that two sections name the same idea, or
        # that a heading is a heading. Reading it in blind windows is what
        # produced 82 concepts, 12 disconnected pieces and contradictory edge
        # directions from a 1,881-token PDF. conceptmap.py has the detail, and
        # handles a document too long to take in one piece.
        cmap = await asyncio.to_thread(conceptmap.build_map, text)
        result.triples = len(cmap.links) + sum(len(c.requires) for c in cmap.concepts)
        if not cmap.concepts:
            raise ValueError("no concepts could be read from this document")

        if job is not None:
            job.progress = 70
            await db.commit()

        result.entities = await queries.write_concept_map(
            document.workspace_id,
            document.id,
            cmap,
            [{"id": c.chunk_id, "text": c.text} for c in chunks],
        )
        log.info(
            "map: %s concept(s), %s link(s), %s contrast(s)",
            len(cmap.concepts), len(cmap.links), len(cmap.contrasts),
        )

        # --- embed -------------------------------------------------------
        # Chunk vectors power chat's text search. Done at ingest so a question
        # never waits on embedding, and so the graph and its text index are
        # always written together.
        try:
            embedded = await vectors.embed_document_chunks(
                document.workspace_id, document.id
            )
            log.info("embedded %s chunk(s) for search", embedded)
        except Exception:
            # Chat degrades to graph-only retrieval; ingestion still succeeded.
            log.exception("chunk embedding failed for %s", document.id)

        # --- resolve -----------------------------------------------------
        # Runs across the whole workspace, not just this document: the
        # duplicates worth merging are the ones spanning documents, which is
        # what turns several per-document subgraphs into one usable graph.
        await _set_status(db, document, DocumentStatus.resolving, job, progress=90)
        stats = await resolution.resolve_workspace(document.workspace_id)
        result.merged = stats["merged"]
        log.info(
            "resolution: examined %s entities, merged %s into %s groups",
            stats["examined"], stats["merged"], stats["groups"],
        )

        await _set_status(db, document, DocumentStatus.ready, job, progress=100)

    except Exception as exc:
        log.exception("ingestion failed for document %s", document.id)
        document.status = DocumentStatus.failed
        # Message is safe to show: our own exceptions, not driver internals.
        document.error = f"{type(exc).__name__}: {exc}"[:2000]
        if job is not None:
            job.status = JobStatus.failed
            job.error = document.error
        await db.commit()
        raise

    return result


def _chunk_row(chunk: Chunk) -> dict:
    return {
        "id": chunk.chunk_id,
        "text": chunk.text,
        "ord": chunk.ord,
        "page": chunk.page,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
    }


async def _set_status(
    db: AsyncSession,
    document: Document,
    status: DocumentStatus,
    job: Job | None,
    progress: int,
) -> None:
    document.status = status
    if job is not None:
        job.status = (
            JobStatus.done if status is DocumentStatus.ready else JobStatus.running
        )
        job.progress = progress
    await db.commit()


async def ingest_repo(
    db: AsyncSession, document_id: uuid.UUID, url: str, token: str | None = None
) -> IngestResult:
    """Ingest a GitHub repository as one document.

    Files are concatenated with path headers so the chunker keeps a boundary at
    each file and a chunk's text still points back at a real location.
    """
    document, job = await _load(db, document_id)
    await _set_status(db, document, DocumentStatus.parsing, job, progress=5)

    try:
        files = await parsers.fetch_repo_files(url, token)
    except Exception as exc:
        document.status = DocumentStatus.failed
        document.error = f"{type(exc).__name__}: {exc}"[:2000]
        if job is not None:
            job.status = JobStatus.failed
            job.error = document.error
        await db.commit()
        raise

    if not files:
        raise ValueError("no readable text or code files found in that repository")

    # A "# path" line before each file gives the chunker a heading to split on,
    # so chunks stay aligned to file boundaries instead of straddling them.
    combined = "\n\n".join(f"# {path}\n\n{content}" for path, content in files)
    document.size_bytes = len(combined.encode("utf-8"))

    # Straight to _ingest_text: this is already text, and running it back
    # through the parser is what broke repo ingestion the first time.
    return await _ingest_text(
        db, document, job, combined[: _cfg().limits.max_extracted_chars]
    )
