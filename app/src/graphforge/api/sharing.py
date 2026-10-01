from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.api.deps import (
    require_csrf,
    require_workspace_owner,
)
from graphforge.db.database import get_db
from graphforge.db.models import Workspace


router = APIRouter(
    prefix="/api/w/{workspace_id}",
    dependencies=[Depends(require_csrf)],
)


class SharingUpdate(BaseModel):
    role: str


@router.get("/sharing")
async def get_sharing(
    workspace: Workspace = Depends(require_workspace_owner),
) -> dict:
    role = workspace.share_role
    if not role or role not in {"viewer", "editor"}:
        role = "none"
    return {"role": role}


@router.patch("/sharing")
async def update_sharing(
    body: SharingUpdate,
    workspace: Workspace = Depends(require_workspace_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:

    role = body.role.strip().lower()

    if role not in {"viewer", "editor", "none"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be viewer, editor, or none.",
        )

    # "none" means private — clear the share_role so require_workspace denies
    # any non-owner. Store the string "none" rather than NULL so the column
    # stays NOT NULL and the frontend can round-trip the value correctly.
    workspace.share_role = role if role != "none" else "none"

    await db.commit()
    await db.refresh(workspace)

    return {
        "ok": True,
        "role": workspace.share_role if workspace.share_role in {"viewer", "editor"} else "none",
    }