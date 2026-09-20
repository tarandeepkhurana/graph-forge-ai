"""Neo4j access, with tenant isolation enforced in one place.

Neo4j Community has no per-tenant databases, so every node carries a
`workspace_id` and every query must filter on it. Enforcing that in each of
fifty handlers means it gets forgotten in the fifty-first -- and forgetting it
once leaks one user's documents to another, the worst failure this app has.

So the rule is mechanical: `run()` refuses any Cypher that does not both take a
`workspace_id` parameter and mention it in the query text. It is a blunt check,
deliberately: a blunt check that always runs beats a careful one that sometimes
does not.

Cypher is always parameterised. Labels and relationship types cannot be
parameterised in Cypher, so any that vary come from a fixed allowlist in
`graphforge.extraction.schema`, never from user strings.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncio

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from graphforge.core.config import get_settings

log = logging.getLogger(__name__)

_driver = None


def get_driver():
    """Create the driver on first use, not at import.

    Connecting at import time makes this module impossible to import without a
    live Neo4j and full configuration, which breaks tests and any tooling that
    only wants to read the Cypher.
    """
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=20,
        )
    return _driver


class TenantScopeError(RuntimeError):
    """Raised when a query would run without workspace scoping."""


async def close() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def run(
    cypher: str,
    workspace_id: uuid.UUID | str,
    /,
    **params: Any,
) -> list[dict]:
    """Run a workspace-scoped read/write query.

    `workspace_id` is positional and mandatory so it cannot be forgotten in a
    keyword splat.
    """
    if "$workspace_id" not in cypher:
        raise TenantScopeError(
            "query does not reference $workspace_id; every query must be "
            f"workspace-scoped. Query began: {cypher[:120]!r}"
        )

    params["workspace_id"] = str(workspace_id)
    return await _run_with_retry(cypher, params)


# An Aura Free instance pauses when idle and takes a moment to wake, during
# which the driver reports a routing failure or a defunct connection. Those are
# transient: retrying a moment later succeeds. Without this a paused database
# turns into "something went wrong" on the user's screen for a blip.
_RETRYABLE = (ServiceUnavailable, SessionExpired)
_RETRIES = 3
_BACKOFF_SECONDS = 1.5


async def _run_with_retry(cypher: str, params: dict) -> list[dict]:
    last: Exception | None = None

    for attempt in range(_RETRIES):
        try:
            async with get_driver().session() as session:
                result = await session.run(cypher, **params)
                return [record.data() async for record in result]
        except _RETRYABLE as exc:
            last = exc
            if attempt == _RETRIES - 1:
                break
            wait = _BACKOFF_SECONDS * (attempt + 1)
            log.warning(
                "neo4j unavailable (%s); retrying in %.1fs", type(exc).__name__, wait
            )
            await asyncio.sleep(wait)
        except Neo4jError:
            # A query error will not fix itself on a retry, and driver messages
            # can echo query text and data -- so log it and stop.
            log.exception("neo4j query failed")
            raise

    log.error("neo4j still unavailable after %s attempts", _RETRIES)
    raise last


async def run_unscoped_admin(cypher: str, **params: Any) -> list[dict]:
    """Escape hatch for schema setup only (constraints, indexes).

    Deliberately named so that it stands out in review and in a grep. Never call
    it from a request handler.
    """
    async with get_driver().session() as session:
        result = await session.run(cypher, **params)
        return [record.data() async for record in result]


# Uniqueness constraints double as indexes and stop duplicate nodes from
# concurrent ingestion jobs racing each other.
SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT entity_id IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS "
    "FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT document_id IF NOT EXISTS "
    "FOR (d:Document) REQUIRE d.id IS UNIQUE",
    # The hot path: "give me this workspace's graph".
    "CREATE INDEX entity_workspace IF NOT EXISTS "
    "FOR (e:Entity) ON (e.workspace_id)",
    "CREATE INDEX chunk_workspace IF NOT EXISTS "
    "FOR (c:Chunk) ON (c.workspace_id)",
    "CREATE INDEX document_workspace IF NOT EXISTS "
    "FOR (d:Document) ON (d.workspace_id)",
    # Entity resolution looks up by canonical name within a workspace.
    "CREATE INDEX entity_canonical IF NOT EXISTS "
    "FOR (e:Entity) ON (e.workspace_id, e.canonical_name)",
    # Vector search over chunk text, for chat questions the graph cannot answer
    # by traversal alone. Defined here rather than in vectors.py so all schema
    # creation stays in one place.
    """
    CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS
    FOR (c:Chunk) ON (c.embedding)
    OPTIONS {indexConfig: {
        `vector.dimensions`: 384,
        `vector.similarity_function`: 'cosine'
    }}
    """,
]


async def init_schema() -> None:
    for statement in SCHEMA_STATEMENTS:
        await run_unscoped_admin(statement)
