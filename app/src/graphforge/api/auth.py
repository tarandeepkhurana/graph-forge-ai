"""Sign up, sign in, sign out (feature 1).

Two behaviours here are deliberate and look like bugs if you skim them:

* Sign-in returns the **same message and takes the same time** whether the email
  is unknown or the password is wrong. Otherwise the endpoint answers "does this
  person have an account here?" for anyone who asks.
* Sign-up returns success even when the email is already registered. The
  alternative -- "that email is taken" -- is an account-existence oracle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.core import security, sessions
from graphforge.core.config import get_settings
from graphforge.core.ratelimit import limiter
from graphforge.db.database import get_db
from graphforge.db.models import AuditLog
from graphforge.db.models import Session as SessionRow
from graphforge.db.models import User, Workspace

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
def _cfg():
    return get_settings()

GENERIC_LOGIN_FAILURE = "Email or password is incorrect."


class Credentials(BaseModel):
    email: EmailStr
    # 12 chars minimum: length beats composition rules for real-world strength.
    password: str = Field(min_length=12, max_length=200)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    common = {
        "secure": _cfg().cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": _cfg().session_ttl_hours * 3600,
    }
    # HttpOnly: JavaScript cannot read it, so an XSS bug cannot steal the session.
    response.set_cookie(_cfg().session_cookie, token, httponly=True, **common)
    # The CSRF cookie must be readable by our own JS to echo it in the header.
    response.set_cookie(security.CSRF_COOKIE, csrf, httponly=False, **common)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await limiter.check(f"signup:{_client_ip(request)}", limit=5, window_seconds=3600)

    # Validated by hand rather than as a body model, because the fields arrive as
    # form data. FastAPI turns a *declared* model's ValidationError into a 422,
    # but one raised inside the handler escapes as a 500 -- so it is caught here
    # and turned into the message the user actually needs.
    try:
        creds = Credentials(email=email, password=password)
    except ValidationError as exc:
        problems = {e["loc"][0] for e in exc.errors()}
        if "password" in problems:
            detail = "Password must be at least 12 characters."
        else:
            detail = "That does not look like a valid email address."
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail) from exc

    existing = (
        await db.execute(select(User).where(User.email == creds.email.lower()))
    ).scalar_one_or_none()

    if existing is None:
        user = User(
            email=creds.email.lower(),
            password_hash=security.hash_password(creds.password),
        )
        db.add(user)
        await db.flush()
        # Every user starts with somewhere to put documents.
        db.add(Workspace(owner_id=user.id, name="My workspace"))
        db.add(AuditLog(user_id=user.id, action="signup", ip=_client_ip(request)))
        await db.commit()
    else:
        # Burn comparable time so response timing does not reveal the difference.
        security.waste_time_like_a_real_verify()

    return {"ok": True, "message": "Check your email to confirm your account."}


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ip = _client_ip(request)
    await limiter.check(f"login:{ip}", limit=10, window_seconds=900)

    user = (
        await db.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()

    if user is None:
        security.waste_time_like_a_real_verify()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_LOGIN_FAILURE)

    if not user.is_active or not security.verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_LOGIN_FAILURE)

    # Transparently upgrade the hash if argon2 parameters have been raised.
    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)

    token = security.new_session_token()
    csrf = security.new_csrf_token()
    db.add(
        SessionRow(
            user_id=user.id,
            token_hash=security.hash_session_token(token),
            csrf_token=csrf,
            expires_at=datetime.now(timezone.utc)
            + timedelta(hours=_cfg().session_ttl_hours),
            user_agent=request.headers.get("user-agent", "")[:300],
            ip=ip,
        )
    )
    db.add(AuditLog(user_id=user.id, action="login", ip=ip))
    await db.commit()

    _set_session_cookies(response, token, csrf)
    return {"ok": True}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = request.cookies.get(_cfg().session_cookie)
    if token:
        # Drop the cached identity first. Signing out has to take effect now,
        # not when a cache entry happens to expire -- that immediacy is the
        # reason sessions live server-side instead of in a JWT.
        sessions.forget(security.hash_session_token(token))
        row = (
            await db.execute(
                select(SessionRow).where(
                    SessionRow.token_hash == security.hash_session_token(token)
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            # Server-side delete: the session is dead immediately, not at expiry.
            await db.delete(row)
            await db.commit()

    response.delete_cookie(_cfg().session_cookie, path="/")
    response.delete_cookie(security.CSRF_COOKIE, path="/")
    return {"ok": True}
