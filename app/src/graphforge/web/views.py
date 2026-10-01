"""HTML pages.

Server-rendered, one origin with the API. The only JSON the page ships with is
a small config block the canvas reads; everything else it fetches.
"""

from __future__ import annotations

import json

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.api.deps import current_user_optional, require_workspace
from graphforge.core.config import get_limits
from graphforge.db.database import get_db
from graphforge.db.models import Document, User, Workspace
from graphforge.web import quiz_views
from graphforge.extraction.schema import ENTITY_SHAPES, RELATION_TYPES, _SPARE_SHAPES

router = APIRouter(include_in_schema=False)


@router.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request, user: User | None = Depends(current_user_optional)):
    if user is not None:
        return RedirectResponse("/", status_code=303)

    from graphforge.main import templates

    return templates.TemplateResponse(
        request, "login.html", {"csp_nonce": getattr(request.state, "csp_nonce", "")}
    )


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: User | None = Depends(current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        # Someone arriving for the first time gets told what this is, rather
        # than dropped straight onto a login form.
        from graphforge.main import templates

        return templates.TemplateResponse(
            request, "home.html", {"csp_nonce": getattr(request.state, "csp_nonce", "")}
        )

    next_path = request.cookies.get(quiz_views.NEXT_COOKIE)
    if next_path:
        response = RedirectResponse(
            next_path if quiz_views.is_safe_next(next_path) else "/", status_code=303
        )
        response.delete_cookie(quiz_views.NEXT_COOKIE)
        return response

    workspace = (
        await db.execute(
            select(Workspace)
            .where(Workspace.owner_id == user.id)
            .order_by(Workspace.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    if workspace is None:
        # A user always has somewhere to work; signup creates one, but a row
        # deleted by hand should not strand them on a blank page.
        workspace = Workspace(owner_id=user.id, name="My workspace")
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

    documents = (
        (
            await db.execute(
                select(Document)
                .where(Document.workspace_id == workspace.id)
                .order_by(Document.created_at)
            )
        )
        .scalars()
        .all()
    )

    from graphforge.main import templates

    config = {
        "workspaceId": str(workspace.id),
        "workspaceOwner": str(workspace.owner_id) == str(user.id),
        "workspaceRole": "owner",
        "shapes": ENTITY_SHAPES,
        # Types are open now, so the canvas needs a pool to assign from when it
        # meets one it does not know.
        "spareShapes": _SPARE_SHAPES,
        "minConfidence": get_limits().min_confidence,
        "relationTypes": RELATION_TYPES,
    }

    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "workspace": workspace,
            "documents": documents,
            "config_json": json.dumps(config),
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
        },
    )


@router.get("/w/{workspace_id}", response_class=HTMLResponse)
async def workspace_page(
    request: Request,
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Open any workspace by its ID.

    The owner lands here directly; for shared workspaces, `require_workspace`
    already checked that the logged-in user is allowed to see it.
    """
    documents = (
        (
            await db.execute(
                select(Document)
                .where(Document.workspace_id == workspace.id)
                .order_by(Document.created_at)
            )
        )
        .scalars()
        .all()
    )

    from graphforge.main import templates

    # Determine caller's role so the UI can show/hide edit controls.
    role = getattr(request.state, "workspace_role", "viewer")
    is_owner = role == "owner"

    config = {
        "workspaceId": str(workspace.id),
        "workspaceOwner": is_owner,
        "workspaceRole": role,
        "shapes": ENTITY_SHAPES,
        "spareShapes": _SPARE_SHAPES,
        "minConfidence": get_limits().min_confidence,
        "relationTypes": RELATION_TYPES,
    }

    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "workspace": workspace,
            "documents": documents,
            "config_json": json.dumps(config),
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
        },
    )
