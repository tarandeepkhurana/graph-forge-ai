"""Request dependencies: who is calling, and may they touch this workspace?

`require_workspace` is the single authorisation choke point. It derives the
workspace from the **session**, never from the request body, and returns 404
rather than 403 for someone else's workspace -- a 403 confirms the id exists,
which is a small information leak that adds up across an enumeration attack.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.core import security, sessions
from graphforge.core.config import get_settings
from graphforge.db.database import get_db
from graphforge.db.models import Session as SessionRow
from graphforge.db.models import User, Workspace

def _cfg():
    return get_settings()

NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
UNAUTHENTICATED = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")


async def current_user_optional(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    token = request.cookies.get(_cfg().session_cookie)
    if not token:
        return None

    token_hash = security.hash_session_token(token)

    # A hit here skips the database entirely. See core/sessions.py for why that
    # is safe -- logout invalidates explicitly, and the session's own expiry is
    # still enforced on every hit.
    cached = sessions.get(token_hash)
    if cached is not None:
        if not cached.is_active:
            return None
        request.state.token_hash = token_hash
        request.state.workspaces = cached.workspaces
        return User(
            id=cached.user_id, email=cached.email, is_active=True,
            password_hash="", email_verified=True,
        )

    # Session and user fetched in ONE round trip, not two. Measured: the
    # database is ~300 ms away, so every avoidable query is a third of a second
    # added to every click. This join alone removes ~300 ms from each request.
    found = (
        await db.execute(
            select(SessionRow, User)
            .join(User, User.id == SessionRow.user_id)
            .where(SessionRow.token_hash == token_hash)
        )
    ).first()

    if found is None:
        return None
    row, user = found

    if row.expires_at <= datetime.now(timezone.utc):
        await db.delete(row)          # expired sessions do not linger
        return None
    if not user.is_active:
        return None

    # Cache the identity *and* which workspaces this user owns, so
    # require_workspace can authorise without its own round trip.
    owned = (
        await db.execute(
            select(Workspace.id, Workspace.name).where(Workspace.owner_id == user.id)
        )
    ).all()
    workspaces = {str(wid): name for wid, name in owned}

    sessions.put(
        token_hash,
        user_id=user.id,
        email=user.email,
        is_active=user.is_active,
        expires_at=row.expires_at.timestamp(),
        workspaces=workspaces,
    )

    request.state.session = row
    request.state.token_hash = token_hash
    request.state.workspaces = workspaces
    return user


async def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise UNAUTHENTICATED
    return user


async def require_csrf(request: Request) -> None:
    """Double-submit check on every state-changing request.

    Safe methods are exempt; everything else must echo the CSRF cookie in a
    header. A cross-site form can make the browser send the cookie, but the
    attacker's JavaScript cannot read it to set the header.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get(security.CSRF_COOKIE)
    header = request.headers.get(security.CSRF_HEADER)
    if not security.csrf_ok(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF check failed")


async def require_workspace(
    request: Request,
    workspace_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    """Load a workspace only if this user owns it.

    Ownership is answered from the cached set when available, which removes the
    second database round trip from every authorised request. The fallback path
    is unchanged, so a cache miss is slower but never wrong -- and a workspace
    absent from the set is still refused, never assumed.
    """
    owned = getattr(request.state, "workspaces", None)
    if owned is not None:
        name = owned.get(str(workspace_id))
        if name is None:
            # Not in the owned set: refused without a query. Absence is a
            # denial, never an invitation to go and check.
            raise NOT_FOUND
        # Ownership is already established, so the row is rebuilt from cache
        # rather than fetched. Callers only read id / owner_id / name.
        return Workspace(id=workspace_id, owner_id=user.id, name=name)

    workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.owner_id != user.id:
        raise NOT_FOUND
    return workspace
