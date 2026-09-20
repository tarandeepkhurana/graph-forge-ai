"""Cypher for the graph: writing extraction output, reading it back, editing it.

Two things shape this file.

**Grounding.** Every entity keeps `MENTIONED_IN` edges to the chunks it came
from. Clicking a node in the editor is one hop along those edges, which is how
the side panel shows text the user actually uploaded rather than a summary.

**Subgraph per document (decision D8).** Nodes record which documents they came
from, so re-ingesting one file rebuilds only its own contribution and leaves the
rest of the workspace alone.
"""

from __future__ import annotations

import json
import uuid

from graphforge.extraction.schema import Triple, normalize, normalize_type
from graphforge.graph import client


def _span_key(span: tuple[int, int] | None) -> str | None:
    """Encode a character range as one value, so it dedupes as a unit."""
    return None if span is None else f"{span[0]}-{span[1]}"


def _entity_id(workspace_id: str, canonical: str, etype: str) -> str:
    """Stable id so the same entity from two documents becomes one node."""
    return f"{workspace_id}:{etype}:{canonical}"


async def upsert_document(
    workspace_id: uuid.UUID, document_id: uuid.UUID, name: str
) -> None:
    await client.run(
        """
        MERGE (d:Document {id: $document_id})
        SET d.workspace_id = $workspace_id, d.name = $name
        """,
        workspace_id,
        document_id=str(document_id),
        name=name,
    )


async def write_chunks(
    workspace_id: uuid.UUID, document_id: uuid.UUID, chunks: list[dict]
) -> None:
    """Store chunk text and offsets -- the grounding payload for the side panel."""
    await client.run(
        """
        MATCH (d:Document {id: $document_id, workspace_id: $workspace_id})
        UNWIND $chunks AS c
        MERGE (chunk:Chunk {id: c.id})
        SET chunk.workspace_id = $workspace_id,
            chunk.text        = c.text,
            chunk.ord         = c.ord,
            chunk.page        = c.page,
            chunk.char_start  = c.char_start,
            chunk.char_end    = c.char_end
        MERGE (chunk)-[:PART_OF]->(d)
        """,
        workspace_id,
        document_id=str(document_id),
        chunks=chunks,
    )


async def write_triples(
    workspace_id: uuid.UUID, document_id: uuid.UUID, triples: list[Triple]
) -> int:
    """Write entities and relations, merging with anything already there.

    The relation *type* is stored as a property rather than as the Cypher
    relationship type. Cypher cannot parameterise a relationship type, so making
    it dynamic would mean string-building the query from model output -- a Cypher
    injection hole. `REL` with a `type` property keeps every query parameterised.
    """
    ws = str(workspace_id)
    rows = []
    for t in triples:
        # Types are normalised, not filtered. Rejecting an unlisted relation
        # silently threw the triple away -- the model would find something real,
        # name it with a verb we had not thought of, and the fact vanished with
        # no error anywhere. Safe to keep: the type is a property value, always
        # parameterised, never part of the query text.
        predicate = normalize_type(t.predicate, "related_to")
        s_canon, o_canon = normalize(t.subject), normalize(t.object)
        if not s_canon or not o_canon:
            continue
        rows.append(
            {
                "s_id": _entity_id(ws, s_canon, normalize_type(t.subject_type)),
                "s_name": t.subject,
                "s_canon": s_canon,
                "s_type": normalize_type(t.subject_type),
                "o_id": _entity_id(ws, o_canon, normalize_type(t.object_type)),
                "o_name": t.object,
                "o_canon": o_canon,
                "o_type": normalize_type(t.object_type),
                "rel_type": predicate,
                "confidence": t.confidence,
                "chunk_id": t.chunk_id,
                # "start-end" strings rather than two parallel lists: Cypher's
                # CASE would dedupe starts and ends independently and silently
                # misalign them.
                "s_span": _span_key(t.subject_span),
                "o_span": _span_key(t.object_span),
            }
        )

    if not rows:
        return 0

    await client.run(
        """
        UNWIND $rows AS r
        MERGE (s:Entity {id: r.s_id})
          ON CREATE SET s.name = r.s_name, s.created_from = $document_id
        SET s.workspace_id = $workspace_id,
            s.canonical_name = r.s_canon,
            s.type = r.s_type,
            s.documents = CASE
                WHEN s.documents IS NULL THEN [$document_id]
                WHEN $document_id IN s.documents THEN s.documents
                ELSE s.documents + $document_id END

        MERGE (o:Entity {id: r.o_id})
          ON CREATE SET o.name = r.o_name, o.created_from = $document_id
        SET o.workspace_id = $workspace_id,
            o.canonical_name = r.o_canon,
            o.type = r.o_type,
            o.documents = CASE
                WHEN o.documents IS NULL THEN [$document_id]
                WHEN $document_id IN o.documents THEN o.documents
                ELSE o.documents + $document_id END

        MERGE (s)-[rel:REL {type: r.rel_type}]->(o)
          ON CREATE SET rel.user_edited = false
        SET rel.workspace_id = $workspace_id,
            rel.confidence = CASE
                WHEN rel.confidence IS NULL OR r.confidence > rel.confidence
                THEN r.confidence ELSE rel.confidence END,
            rel.source_chunk_ids = CASE
                WHEN rel.source_chunk_ids IS NULL THEN [r.chunk_id]
                WHEN r.chunk_id IN rel.source_chunk_ids THEN rel.source_chunk_ids
                ELSE rel.source_chunk_ids + r.chunk_id END

        WITH s, o, r
        MATCH (c:Chunk {id: r.chunk_id, workspace_id: $workspace_id})
        MERGE (s)-[ms:MENTIONED_IN]->(c)
        SET ms.spans = CASE
            WHEN r.s_span IS NULL THEN ms.spans
            WHEN ms.spans IS NULL THEN [r.s_span]
            WHEN r.s_span IN ms.spans THEN ms.spans
            ELSE ms.spans + r.s_span END
        MERGE (o)-[mo:MENTIONED_IN]->(c)
        SET mo.spans = CASE
            WHEN r.o_span IS NULL THEN mo.spans
            WHEN mo.spans IS NULL THEN [r.o_span]
            WHEN r.o_span IN mo.spans THEN mo.spans
            ELSE mo.spans + r.o_span END
        """,
        workspace_id,
        document_id=str(document_id),
        rows=rows,
    )
    return len(rows)


def _ground(quote: str, chunks: list[dict]) -> tuple[str | None, str | None]:
    """Find the passage a concept's quote came from.

    The map is generated from the whole document at once, so a concept does not
    arrive attached to a chunk the way a per-chunk triple did. The quote is
    asked for verbatim precisely so it can be found again here -- that is what
    keeps the side panel grounded in the document rather than in the model.

    Whitespace is normalised on both sides before matching: a PDF wraps lines
    wherever the column ended, so the same sentence differs from the model's
    copy of it by newlines alone. Falls back to the longest run of words that
    does match, because a quote clipped at 25 words often ends mid-phrase.
    """
    if not quote:
        return None, None

    def flat(text: str) -> str:
        return " ".join(text.split())

    needle = flat(quote)
    for chunk in chunks:
        hay = flat(chunk["text"])
        at = hay.find(needle)
        if at >= 0:
            return chunk["id"], f"{at}-{at + len(needle)}"

    # Nothing matched whole; try progressively shorter prefixes of the quote.
    words = needle.split()
    for length in range(len(words) - 1, 3, -1):
        part = " ".join(words[:length])
        for chunk in chunks:
            at = flat(chunk["text"]).find(part)
            if at >= 0:
                return chunk["id"], f"{at}-{at + len(part)}"
    return None, None


async def write_concept_map(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    cmap,
    chunks: list[dict],
) -> int:
    """Write one document's learning map.

    Concepts stay `:Entity` and links stay `:REL` so that everything already
    built on them -- editing, deletion, resolution, chat retrieval, the vector
    index -- keeps working unchanged. What is new is that a concept now carries
    what the document says about it, and that prerequisites are stored as
    ordinary links with the type `requires`, so the canvas can draw the reading
    order without a second edge table.

    `contrasts` goes on the document as JSON. Neo4j has no nested-map property,
    and a contrast belongs to the document rather than to any one concept in
    it -- the panel shows a concept the rows it appears in.
    """
    ws = str(workspace_id)
    rows = []
    for concept in cmap.concepts:
        canonical = normalize(concept.name)
        if not canonical:
            continue
        chunk_id, span = _ground(concept.quote, chunks)
        rows.append(
            {
                "id": _entity_id(ws, canonical, "concept"),
                "name": concept.name,
                "canon": canonical,
                "one_liner": concept.one_liner,
                "importance": concept.importance,
                "how_it_works": concept.how_it_works,
                "strengths": concept.strengths,
                "limitations": concept.limitations,
                "when_to_use": concept.when_to_use,
                "examples": concept.examples,
                "quote": concept.quote,
                "chunk_id": chunk_id,
                "span": span,
            }
        )
    if not rows:
        return 0

    await client.run(
        """
        UNWIND $rows AS r
        MERGE (e:Entity {id: r.id})
          ON CREATE SET e.created_from = $document_id
        SET e.workspace_id = $workspace_id,
            e.name = r.name,
            e.canonical_name = r.canon,
            e.type = 'concept',
            e.one_liner = r.one_liner,
            e.importance = r.importance,
            e.how_it_works = r.how_it_works,
            e.strengths = r.strengths,
            e.limitations = r.limitations,
            e.when_to_use = r.when_to_use,
            e.examples = r.examples,
            e.quote = r.quote,
            e.documents = CASE
                WHEN e.documents IS NULL THEN [$document_id]
                WHEN $document_id IN e.documents THEN e.documents
                ELSE e.documents + $document_id END
        WITH e, r WHERE r.chunk_id IS NOT NULL
        MATCH (c:Chunk {id: r.chunk_id, workspace_id: $workspace_id})
        MERGE (e)-[m:MENTIONED_IN]->(c)
        SET m.spans = CASE
            WHEN r.span IS NULL THEN m.spans
            WHEN m.spans IS NULL THEN [r.span]
            WHEN r.span IN m.spans THEN m.spans
            ELSE m.spans + r.span END
        """,
        workspace_id,
        document_id=str(document_id),
        rows=rows,
    )

    # Links the document states, plus prerequisites as links of type `requires`.
    edges = [
        {
            "s": _entity_id(ws, normalize(link.source), "concept"),
            "o": _entity_id(ws, normalize(link.target), "concept"),
            "type": link.relation,
            "why": link.why,
            "prerequisite": False,
        }
        for link in cmap.links
    ]
    # A prerequisite is not a second arrow. Where the document already states a
    # link between the same two concepts in the same direction, being a
    # prerequisite is a property of THAT link -- "partial_fit enables Online
    # Learning" and "learn Online Learning first" are two things to know about
    # one connection, and drawing them as two parallel arrows only clutters the
    # map. A standalone `requires` edge is created only when nothing else
    # connects the pair.
    stated = {(e["s"], e["o"]): e for e in edges}
    for concept in cmap.concepts:
        source = _entity_id(ws, normalize(concept.name), "concept")
        for need in concept.requires:
            target = _entity_id(ws, normalize(need), "concept")
            existing = stated.get((source, target))
            if existing is not None:
                existing["prerequisite"] = True
            else:
                edges.append(
                    {
                        "s": source,
                        "o": target,
                        "type": "requires",
                        "why": "understand this first",
                        "prerequisite": True,
                    }
                )
    if edges:
        await client.run(
            """
            UNWIND $edges AS r
            MATCH (s:Entity {id: r.s, workspace_id: $workspace_id})
            MATCH (o:Entity {id: r.o, workspace_id: $workspace_id})
            MERGE (s)-[rel:REL {type: r.type}]->(o)
              ON CREATE SET rel.user_edited = false
            SET rel.workspace_id = $workspace_id,
                rel.why = r.why,
                rel.prerequisite = r.prerequisite,
                rel.confidence = coalesce(rel.confidence, 0.9)
            """,
            workspace_id,
            edges=edges,
        )

    await client.run(
        """
        MATCH (d:Document {id: $document_id, workspace_id: $workspace_id})
        SET d.teaches = $teaches, d.contrasts = $contrasts
        """,
        workspace_id,
        document_id=str(document_id),
        teaches=cmap.teaches,
        contrasts=json.dumps(
            [{"dimension": c.dimension, "values": c.values} for c in cmap.contrasts]
        ),
    )
    return len(rows)


async def get_graph(workspace_id: uuid.UUID, min_confidence: float = 0.0) -> dict:
    """Nodes and edges for the canvas. Text is deliberately not included --
    500 nodes should be 500 names, not 500 passages."""
    nodes = await client.run(
        """
        MATCH (e:Entity {workspace_id: $workspace_id})
        RETURN e.id AS id, e.name AS name, e.type AS type,
               e.color AS color, e.documents AS documents,
               e.one_liner AS one_liner,
               coalesce(e.importance, 'supporting') AS importance,
               coalesce(e.user_edited, false) AS user_edited
        """,
        workspace_id,
    )
    edges = await client.run(
        """
        MATCH (s:Entity {workspace_id: $workspace_id})
              -[r:REL]->
              (o:Entity {workspace_id: $workspace_id})
        WHERE r.confidence >= $min_confidence OR r.user_edited = true
        RETURN s.id AS source, o.id AS target, r.type AS type,
               r.why AS why, coalesce(r.prerequisite, false) AS prerequisite,
               r.confidence AS confidence, r.color AS color,
               r.user_edited AS user_edited
        """,
        workspace_id,
        min_confidence=min_confidence,
    )
    docs = await client.run(
        """
        MATCH (d:Document {workspace_id: $workspace_id})
        RETURN d.id AS id, d.name AS name, d.teaches AS teaches,
               d.contrasts AS contrasts
        """,
        workspace_id,
    )
    return {"nodes": nodes, "edges": edges, "documents": docs}


async def get_node_detail(workspace_id: uuid.UUID, entity_id: str) -> dict | None:
    """The side panel: the node plus the source passages it came from."""
    rows = await client.run(
        """
        MATCH (e:Entity {id: $entity_id, workspace_id: $workspace_id})
        OPTIONAL MATCH (e)-[m:MENTIONED_IN]->(c:Chunk)-[:PART_OF]->(d:Document)
        WITH e, m, c, d ORDER BY d.name, c.ord
        RETURN e.id AS id, e.name AS name, e.type AS type,
               e.notes AS notes, e.color AS color,
               e.one_liner AS one_liner, e.quote AS quote,
               coalesce(e.importance, 'supporting') AS importance,
               coalesce(e.how_it_works, []) AS how_it_works,
               coalesce(e.strengths, []) AS strengths,
               coalesce(e.limitations, []) AS limitations,
               coalesce(e.when_to_use, []) AS when_to_use,
               coalesce(e.examples, []) AS examples,
               collect(CASE WHEN c IS NULL THEN NULL ELSE {
                   chunk_id: c.id, text: c.text, page: c.page,
                   char_start: c.char_start, char_end: c.char_end,
                   document: d.name,
                   spans: coalesce(m.spans, [])
               } END) AS sources
        """,
        workspace_id,
        entity_id=entity_id,
    )
    if not rows:
        return None
    row = rows[0]
    row["sources"] = [s for s in row["sources"] if s]
    return row


async def create_entity(
    workspace_id: uuid.UUID, name: str, etype: str, color: str | None = None
) -> str:
    entity_id = _entity_id(str(workspace_id), normalize(name), etype)
    await client.run(
        """
        MERGE (e:Entity {id: $entity_id})
        SET e.workspace_id = $workspace_id, e.name = $name,
            e.canonical_name = $canonical, e.type = $type,
            e.color = $color, e.user_edited = true
        """,
        workspace_id,
        entity_id=entity_id,
        name=name,
        canonical=normalize(name),
        type=etype,
        color=color,
    )
    return entity_id


async def update_entity(workspace_id: uuid.UUID, entity_id: str, **fields) -> bool:
    """Update only an allowlisted set of properties.

    Without the allowlist, a request body could set `workspace_id` and move a
    node into someone else's graph.
    """
    allowed = {"name", "type", "color", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False

    sets = ", ".join(f"e.{k} = ${k}" for k in updates)
    if "name" in updates:
        sets += ", e.canonical_name = $canonical"
        updates["canonical"] = normalize(str(updates["name"]))

    rows = await client.run(
        f"""
        MATCH (e:Entity {{id: $entity_id, workspace_id: $workspace_id}})
        SET {sets}, e.user_edited = true
        RETURN e.id AS id
        """,
        workspace_id,
        entity_id=entity_id,
        **updates,
    )
    return bool(rows)


async def delete_entity(workspace_id: uuid.UUID, entity_id: str) -> bool:
    rows = await client.run(
        """
        MATCH (e:Entity {id: $entity_id, workspace_id: $workspace_id})
        DETACH DELETE e
        RETURN count(*) AS deleted
        """,
        workspace_id,
        entity_id=entity_id,
    )
    return bool(rows and rows[0].get("deleted"))


async def upsert_edge(
    workspace_id: uuid.UUID,
    source_id: str,
    target_id: str,
    rel_type: str,
    color: str | None = None,
) -> bool:
    rel_type = normalize_type(rel_type, "related_to")
    rows = await client.run(
        """
        MATCH (s:Entity {id: $source_id, workspace_id: $workspace_id})
        MATCH (o:Entity {id: $target_id, workspace_id: $workspace_id})
        MERGE (s)-[r:REL {type: $rel_type}]->(o)
        SET r.workspace_id = $workspace_id, r.user_edited = true,
            r.color = $color,
            r.confidence = coalesce(r.confidence, 1.0)
        RETURN r.type AS type
        """,
        workspace_id,
        source_id=source_id,
        target_id=target_id,
        rel_type=rel_type,
        color=color,
    )
    return bool(rows)


async def set_edge_color(
    workspace_id: uuid.UUID,
    source_id: str,
    target_id: str,
    rel_type: str,
    color: str | None,
) -> bool:
    """Recolour an existing link, matching its type exactly.

    Deliberately not `upsert_edge`. That normalises the type before its MERGE,
    and relation types are now the model's own phrasing -- "contrasts with"
    normalises to "contrasts_with", which matches no existing relationship, so
    MERGE would quietly create a second edge beside the one being recoloured.
    Colouring something must never change what the graph says.
    """
    rows = await client.run(
        """
        MATCH (s:Entity {id: $source_id, workspace_id: $workspace_id})
              -[r:REL {type: $rel_type}]->
              (o:Entity {id: $target_id, workspace_id: $workspace_id})
        SET r.color = $color
        RETURN r.type AS type
        """,
        workspace_id,
        source_id=source_id,
        target_id=target_id,
        rel_type=rel_type,
        color=color,
    )
    return bool(rows)


async def delete_edge(
    workspace_id: uuid.UUID, source_id: str, target_id: str, rel_type: str
) -> bool:
    rows = await client.run(
        """
        MATCH (s:Entity {id: $source_id, workspace_id: $workspace_id})
              -[r:REL {type: $rel_type}]->
              (o:Entity {id: $target_id, workspace_id: $workspace_id})
        DELETE r
        RETURN count(*) AS deleted
        """,
        workspace_id,
        source_id=source_id,
        target_id=target_id,
        rel_type=rel_type,
    )
    return bool(rows and rows[0].get("deleted"))


async def delete_document_subgraph(
    workspace_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Remove one document's contribution, leaving the rest of the workspace intact.

    Entities shared with other documents lose this document from their list but
    survive; entities that came only from here are deleted.
    """
    await client.run(
        """
        MATCH (d:Document {id: $document_id, workspace_id: $workspace_id})
        OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(d)
        DETACH DELETE c
        WITH DISTINCT d
        OPTIONAL MATCH (e:Entity {workspace_id: $workspace_id})
        WHERE $document_id IN e.documents
        SET e.documents = [x IN e.documents WHERE x <> $document_id]
        WITH d, collect(e) AS touched
        // FOREACH rather than UNWIND + WHERE: filtering to the orphaned
        // entities in a WITH drops every row when there are none, and the
        // DETACH DELETE of the document below then never runs -- leaving the
        // document node and its text in the graph after the user deleted it.
        FOREACH (dead IN [x IN touched WHERE size(x.documents) = 0] |
                 DETACH DELETE dead)
        DETACH DELETE d
        """,
        workspace_id,
        document_id=str(document_id),
    )


async def confirm_entity(workspace_id: uuid.UUID, entity_id: str) -> bool:
    """Mark a node -- and the edges touching it -- as human-confirmed.

    This is the product's core gesture: the machine drafted in pencil, the user
    inks it. Confirming a node also inks its edges, because the user is
    asserting the passage's reading as a whole, not just the label of one dot.
    `user_edited` also protects the element from being overwritten by a later
    re-ingestion of the same document.
    """
    rows = await client.run(
        """
        MATCH (e:Entity {id: $entity_id, workspace_id: $workspace_id})
        SET e.user_edited = true
        WITH e
        OPTIONAL MATCH (e)-[r:REL]-(:Entity {workspace_id: $workspace_id})
        SET r.user_edited = true
        RETURN e.id AS id
        """,
        workspace_id,
        entity_id=entity_id,
    )
    return bool(rows)
