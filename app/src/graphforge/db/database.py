"""Async Postgres engine and session factory.

The engine is built on first use rather than at import, for the same reason as
the Neo4j driver: importing a module should never require a reachable database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from graphforge.core.config import get_settings
from graphforge.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # Supabase pools connections itself, so keep our pool small: several app
        # instances with large pools will exhaust the server's connection limit.
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_size=5,
            max_overflow=5,
            # pool_pre_ping issues a liveness SELECT before handing out a
            # pooled connection. Against a database ~300 ms away that is a
            # third of a second added to every request that gets a cold
            # connection. Recycling on an age bound achieves the same thing
            # -- avoiding a server-closed socket -- for free.
            pool_pre_ping=False,
            pool_recycle=1800,
            connect_args={
                # Supabase is reached through the Supavisor pooler, which may
                # hand the same asyncpg connection to a different backend
                # between statements. asyncpg's prepared-statement cache
                # assumes a stable backend, so it has to be off or queries
                # intermittently fail with "prepared statement does not exist".
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction-scoped session per request."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create tables. Fine for development; use migrations before production."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
