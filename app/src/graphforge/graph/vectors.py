"""Chunk embeddings and vector search, inside Neo4j.

Graph traversal alone cannot answer every question. Ask "what does this say
about performance?" and if no entity is named `performance`, there is nothing to
traverse from. Extraction is also only ~40% accurate, so a fact the model missed
is simply absent from the graph -- but it is still sitting in the chunk text.

Vector search covers both gaps. Neo4j has native vector indexes, so the chunks
and their embeddings live in the same database as the graph and a single query
can do both. No second datastore to keep in sync.

The embedding model is the same local MiniLM used for entity resolution: 384
dimensions, already cached, free to run, and nothing leaves the machine.
"""

from __future__ import annotations

import logging
import uuid

from graphforge.graph import client, embeddings

log = logging.getLogger(__name__)

DIMENSIONS = 384  # all-MiniLM-L6-v2
INDEX_NAME = "chunk_embedding"

# Chunks are ~4000 characters, well past the model's 256-token window, so the
# tail of a long chunk would be silently ignored. Embedding a prefix is honest
# about that: it indexes the opening of the passage, which is where a section's
# topic is usually stated.
EMBED_PREFIX_CHARS = 1200

CREATE_INDEX = f"""
CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
FOR (c:Chunk) ON (c.embedding)
OPTIONS {{indexConfig: {{
    `vector.dimensions`: {DIMENSIONS},
    `vector.similarity_function`: 'cosine'
}}}}
"""


async def embed_document_chunks(
    workspace_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    """Embed every chunk of one document and store the vectors on the nodes."""
    rows = await client.run(
        """
        MATCH (c:Chunk {workspace_id: $workspace_id})-[:PART_OF]->
              (d:Document {id: $document_id})
        WHERE c.embedding IS NULL
        RETURN c.id AS id, c.text AS text
        ORDER BY c.ord
        """,
        workspace_id,
        document_id=str(document_id),
    )
    if not rows:
        return 0

    vectors = embeddings.embed([r["text"][:EMBED_PREFIX_CHARS] for r in rows])
    payload = [
        {"id": row["id"], "embedding": [float(x) for x in vector]}
        for row, vector in zip(rows, vectors)
    ]

    await client.run(
        """
        UNWIND $rows AS r
        MATCH (c:Chunk {id: r.id, workspace_id: $workspace_id})
        CALL db.create.setNodeVectorProperty(c, 'embedding', r.embedding)
        """,
        workspace_id,
        rows=payload,
    )
    return len(payload)


async def search_chunks(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    query: str,
    limit: int = 6,
) -> list[dict]:
    """Chunks most similar to `query`, restricted to one document.

    The index is workspace-wide, so it is over-fetched and then filtered down to
    the document. `db.index.vector.queryNodes` cannot take a filter, and asking
    it for exactly `limit` results would return the right count for the wrong
    document.
    """
    vector = embeddings.embed([query])[0]

    return await client.run(
        """
        CALL db.index.vector.queryNodes($index, $probe, $embedding)
        YIELD node AS c, score
        WITH c, score
        WHERE c.workspace_id = $workspace_id
        MATCH (c)-[:PART_OF]->(d:Document {id: $document_id})
        RETURN c.id AS chunk_id, c.text AS text, c.page AS page,
               c.ord AS ord, d.name AS document, score
        ORDER BY score DESC
        LIMIT $limit
        """,
        workspace_id,
        index=INDEX_NAME,
        probe=max(limit * 8, 40),
        embedding=[float(x) for x in vector],
        document_id=str(document_id),
        limit=limit,
    )
