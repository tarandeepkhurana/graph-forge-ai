"""A short-lived cache of resolved sessions.

Every authenticated request used to spend two database round trips before doing
any work: one to resolve the session cookie to a user, one to check the user
owns the workspace in the URL. Measured against this deployment, each costs
~300 ms -- the Postgres instance is in ap-northeast-1 and the app is in India --
so a click on a colour swatch waited ~600 ms on authorisation alone before the
actual update began.

Caching the resolution removes both on a hit. It is a cache of *identity*, not
of data: nothing a user can change through the app is stored here.

**Why this is safe, and where it is not.** The two risks with caching auth are
sessions outliving logout, and permission changes not taking effect. Both are
handled:

  * `forget()` is called on logout, so signing out is immediate rather than
    eventual. That is the property server-side sessions exist to provide, and
    losing it would defeat the point of not using JWTs.
  * The cached expiry is still checked on every hit, so an expired session is
    rejected from cache exactly as it would be from the database.
  * The TTL is deliberately short. Anything not explicitly invalidated is wrong
    for at most `TTL_SECONDS`.

This is per-process. With several app instances each keeps its own, which is
fine for identity but means `forget()` only clears the instance that handled
the logout. Before running more than one instance, this belongs in Redis.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

# Short enough that anything missed by explicit invalidation is stale only
# briefly; long enough to cover a burst of clicks in the editor.
TTL_SECONDS = 30.0


@dataclass
class CachedSession:
    user_id: uuid.UUID
    email: str
    is_active: bool
    expires_at: float          # session expiry, epoch seconds
    workspaces: dict[str, str]      # id -> name, for the ones this user owns
    cached_at: float


_cache: dict[str, CachedSession] = {}


def get(token_hash: str) -> CachedSession | None:
    entry = _cache.get(token_hash)
    if entry is None:
        return None

    now = time.time()
    if now - entry.cached_at > TTL_SECONDS:
        _cache.pop(token_hash, None)
        return None
    # The session's own expiry still applies -- caching must not extend a
    # session past the moment it should have died.
    if entry.expires_at <= now:
        _cache.pop(token_hash, None)
        return None
    return entry


def put(
    token_hash: str,
    user_id: uuid.UUID,
    email: str,
    is_active: bool,
    expires_at: float,
    workspaces: dict[str, str],
) -> None:
    _cache[token_hash] = CachedSession(
        user_id=user_id,
        email=email,
        is_active=is_active,
        expires_at=expires_at,
        workspaces=workspaces,
        cached_at=time.time(),
    )

    # Bound the map. Sessions are small, but an unbounded dict in a
    # long-running process is a slow leak.
    if len(_cache) > 5000:
        now = time.time()
        for key, value in list(_cache.items()):
            if now - value.cached_at > TTL_SECONDS:
                _cache.pop(key, None)


def forget(token_hash: str) -> None:
    """Drop one session. Called on logout so signing out takes effect at once."""
    _cache.pop(token_hash, None)


def forget_user(user_id: uuid.UUID) -> None:
    """Drop every cached session for a user, after a password or status change."""
    for key, value in list(_cache.items()):
        if value.user_id == user_id:
            _cache.pop(key, None)
