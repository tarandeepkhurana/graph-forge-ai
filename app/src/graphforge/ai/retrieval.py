"""Retrieval for chat: find the passages that answer a question about one document.

This is the "local search" half of GraphRAG -- entity-anchored rather than
corpus-level. Four steps, and the middle two are what a plain vector store
cannot do:

    1. link      which entities in the graph does the question name?
    2. expand    what is one hop away from those?      <- relationships
    3. gather    which chunks are those entities mentioned in?
    4. search    plus whatever the question is simply *similar* to

Steps 1-3 answer "how does X relate to Y", which similarity search cannot: the
relationship is an edge, not a resemblance. Step 4 covers what the graph misses,
and it misses plenty -- extraction runs around 40% accurate, so a fact the model
never extracted is absent from the graph while still sitting in the chunk text.

Everything is scoped to a single document, because chat happens on the graph the
user currently has open.

**No LLM runs in this module.** Retrieval is deterministic Cypher and local
embeddings. Only generation sees document text, and it has no tools -- which is
what keeps untrusted document content away from anything that can act.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from graphforge.extraction.schema import normalize
from graphforge.graph import client, vectors

log = logging.getLogger(__name__)

# A question word only counts as naming an entity above this similarity.
LINK_THRESHOLD = 82

MAX_LINKED_ENTITIES = 8
MAX_NEIGHBOURS = 25
MAX_CHUNKS = 8


@dataclass
class Retrieved:
    chunks: list[dict] = field(default_factory=list)
    triples: list[dict] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    # What the map already knows about the matched concepts. Ingestion has
    # already read the whole document and written down what it says about each
    # concept -- re-deriving that from raw passages at question time is both
    # slower and worse, and it is why "what are Online Learning's use cases"
    # was answered from the text while the concept's own `when_to_use` list sat
    # unread in the graph.
    concepts: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        return {
            "entities": len(self.entities),
            "triples": len(self.triples),
            "chunks": len(self.chunks),
            "concepts": len(self.concepts),
        }


async def link_entities(
    workspace_id: uuid.UUID, document_id: uuid.UUID, question: str
) -> list[dict]:
    """Entities from this document whose names appear in the question.

    Matching is fuzzy against the whole question rather than exact, so
    "what does Neo4j use?" still finds the node named "Neo4j" -- and
    "kubernetes" finds "Kubernetes".
    """
    candidates = await client.run(
        """
        MATCH (e:Entity {workspace_id: $workspace_id})-[:MENTIONED_IN]->
              (c:Chunk)-[:PART_OF]->(d:Document {id: $document_id})
        RETURN DISTINCT e.id AS id, e.name AS name, e.type AS type,
               e.one_liner AS one_liner,
               coalesce(e.how_it_works, []) AS how_it_works,
               coalesce(e.strengths, []) AS strengths,
               coalesce(e.limitations, []) AS limitations,
               coalesce(e.when_to_use, []) AS when_to_use,
               coalesce(e.examples, []) AS examples
        """,
        workspace_id,
        document_id=str(document_id),
    )

    question_norm = normalize(question)
    scored: list[tuple[float, dict]] = []

    for entity in candidates:
        name = normalize(entity["name"] or "")
        if not name:
            continue
        # partial_ratio: the entity name is a fragment of a longer question.
        score = fuzz.partial_ratio(name, question_norm)
        # Short names match too easily inside longer words ("AI" inside "explain"),
        # so require them to appear as a whole token.
        if len(name) <= 3 and name not in question_norm.split():
            continue
        if score >= LINK_THRESHOLD:
            scored.append((score, entity))

    scored.sort(key=lambda pair: (pair[0], len(pair[1]["name"] or "")), reverse=True)
    return [entity for _, entity in scored[:MAX_LINKED_ENTITIES]]


async def expand(
    workspace_id: uuid.UUID, document_id: uuid.UUID, entity_ids: list[str]
) -> list[dict]:
    """One hop out from the linked entities: the relationships around them.

    This is the part a vector store has no equivalent of. Both directions are
    followed, because "what uses Neo4j" and "what does Neo4j use" are different
    questions over the same edge.
    """
    if not entity_ids:
        return []

    return await client.run(
        """
        MATCH (e:Entity {workspace_id: $workspace_id})
        WHERE e.id IN $entity_ids
        MATCH (e)-[r:REL]-(other:Entity {workspace_id: $workspace_id})
        MATCH (other)-[:MENTIONED_IN]->(:Chunk)-[:PART_OF]->
              (:Document {id: $document_id})
        WITH DISTINCT startNode(r) AS s, endNode(r) AS o, r
        RETURN s.name AS subject, r.type AS predicate, o.name AS object,
               r.confidence AS confidence
        ORDER BY r.confidence DESC
        LIMIT $limit
        """,
        workspace_id,
        entity_ids=entity_ids,
        document_id=str(document_id),
        limit=MAX_NEIGHBOURS,
    )


async def chunks_for_entities(
    workspace_id: uuid.UUID, document_id: uuid.UUID, entity_ids: list[str]
) -> list[dict]:
    """Passages backing the linked entities, most-mentioned first."""
    if not entity_ids:
        return []

    return await client.run(
        """
        MATCH (e:Entity {workspace_id: $workspace_id})
        WHERE e.id IN $entity_ids
        MATCH (e)-[:MENTIONED_IN]->(c:Chunk)-[:PART_OF]->
              (d:Document {id: $document_id})
        WITH c, d, count(DISTINCT e) AS hits
        RETURN c.id AS chunk_id, c.text AS text, c.page AS page,
               c.ord AS ord, d.name AS document, hits
        ORDER BY hits DESC, c.ord
        LIMIT $limit
        """,
        workspace_id,
        entity_ids=entity_ids,
        document_id=str(document_id),
        limit=MAX_CHUNKS,
    )


async def retrieve(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    question: str,
):
    """Stream retrieval, yielding progress as it happens.

    An async generator rather than a callback: yields ("event", dict) for each
    stage and finally ("result", Retrieved).

    The first version took an `on_event` callback and collected the events into
    a list, because a callback cannot yield out of the enclosing generator. The
    caller then replayed them all at once -- so the interface showed the whole
    pipeline appearing in a single burst after retrieval had already finished,
    which is the opposite of progress reporting. Yielding as we go is what makes
    the trace live.
    """
    result = Retrieved()

    yield ("event", _status("linking", "Finding what your question refers to"))
    linked = await link_entities(workspace_id, document_id, question)
    result.entities = [e["name"] for e in linked]
    # The matched concepts carry what ingestion already worked out about them.
    result.concepts = linked
    entity_ids = [e["id"] for e in linked]

    if linked:
        yield ("event", _status(
            "linked",
            "Matched " + ", ".join(result.entities[:3])
            + (f" and {len(linked) - 3} more" if len(linked) > 3 else ""),
            entities=result.entities,
        ))
    else:
        # Not a failure: the graph holds only what the extractor found, and it
        # misses plenty. Saying so plainly beats "no concepts matched", which
        # reads as though the question went unanswered.
        yield ("event", _status(
            "linked",
            "Nothing in the graph matches that — searching the text instead",
            entities=[],
        ))

    if entity_ids:
        yield ("event", _status("graph", "Following relationships in the graph"))
        result.triples = await expand(workspace_id, document_id, entity_ids)
        yield ("event", _status(
            "graph_done", f"Found {len(result.triples)} relationship(s)"))

    by_id: dict[str, dict] = {}

    if entity_ids:
        for chunk in await chunks_for_entities(workspace_id, document_id, entity_ids):
            by_id[chunk["chunk_id"]] = chunk

    # Always run vector search, even when entities matched: the graph knows
    # what was extracted, the text knows everything else.
    yield ("event", _status("vector", "Searching the document text"))
    try:
        for chunk in await vectors.search_chunks(
            workspace_id, document_id, question, limit=MAX_CHUNKS
        ):
            by_id.setdefault(chunk["chunk_id"], chunk)
    except Exception:
        # Missing embeddings must degrade to graph-only, not fail the question.
        log.exception("vector search failed; continuing with graph results only")

    result.chunks = sorted(by_id.values(), key=lambda c: c.get("ord") or 0)[:MAX_CHUNKS]
    yield ("event", _status(
        "retrieved",
        f"Reading {len(result.chunks)} passage(s)",
        chunks=[
            {
                "chunk_id": c["chunk_id"],
                "document": c.get("document"),
                "page": c.get("page"),
            }
            for c in result.chunks
        ],
    ))
    yield ("result", result)


def _status(stage: str, message: str, **extra) -> dict:
    return {"type": "status", "stage": stage, "message": message, **extra}
