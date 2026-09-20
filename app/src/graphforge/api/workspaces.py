"""Workspace API: documents, the graph, and node/edge editing.

Every route takes `workspace_id` in the path and resolves it through
`require_workspace`, which checks ownership against the session. The client is
never trusted to say which workspace it is in.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.api.deps import require_csrf, require_workspace
from graphforge.core.config import get_limits, get_settings
from graphforge.db.database import get_db, get_session_factory
from graphforge.db.models import Document, DocumentStatus, Job, Workspace
from graphforge.extraction.schema import normalize_type
from graphforge.graph import queries
from graphforge.ingest import parsers, pipeline

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/w/{workspace_id}", dependencies=[Depends(require_csrf)])


# --------------------------------------------------------------- documents --
@router.get("/documents")
async def list_documents(
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Poll target for ingestion progress."""
    rows = (
        await db.execute(
            select(Document, Job)
            .outerjoin(Job, Job.document_id == Document.id)
            .where(Document.workspace_id == workspace.id)
            .order_by(Document.created_at)
        )
    ).all()

    return [
        {
            "id": str(doc.id),
            "filename": doc.filename,
            "status": doc.status.value,
            "pages": doc.pages,
            "progress": job.progress if job else (100 if doc.status is DocumentStatus.ready else 0),
            "error": doc.error,
        }
        for doc, job in rows
    ]


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
) -> dict:
    limits = get_limits()

    count = len(
        (
            await db.execute(
                select(Document.id).where(Document.workspace_id == workspace.id)
            )
        ).all()
    )
    if count >= limits.max_documents_per_workspace:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This workspace already holds {limits.max_documents_per_workspace} documents.",
        )

    # Read with a hard ceiling: never let an upload size the read.
    data = await file.read(limits.max_upload_bytes + 1)
    if len(data) > limits.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Files must be under {limits.max_upload_bytes // 1024 // 1024} MB.",
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is empty.")

    try:
        mime = parsers.sniff_mime(data, file.filename or "")
    except parsers.UnsupportedFile as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    digest = hashlib.sha256(data).hexdigest()
    existing = (
        await db.execute(
            select(Document).where(
                Document.workspace_id == workspace.id, Document.sha256 == digest
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That document is already in this workspace."
        )

    # Opaque storage key: the user's filename never reaches the filesystem.
    storage_key = f"{workspace.id}/{uuid.uuid4().hex}"
    target = Path(get_settings().storage_dir) / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    document = Document(
        workspace_id=workspace.id,
        filename=(file.filename or "untitled")[:400],
        mime=mime,
        sha256=digest,
        size_bytes=len(data),
        storage_key=storage_key,
        status=DocumentStatus.pending,
    )
    db.add(document)
    await db.flush()
    db.add(Job(document_id=document.id, user_id=workspace.owner_id))
    await db.commit()

    background.add_task(_run_ingestion, document.id, data)

    return {"id": str(document.id), "filename": document.filename, "status": "pending"}


class RepoImport(BaseModel):
    url: str = Field(max_length=300)


@router.post("/documents/repo", status_code=status.HTTP_201_CREATED)
async def import_repo(
    body: RepoImport,
    background: BackgroundTasks,
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Import a public GitHub repository as one document.

    The URL is validated here, before any work is queued, so a hostile URL is
    rejected with a clear message rather than failing inside a background task
    where the user only sees "failed". `parse_github_url` allowlists
    github.com -- see parsers.py for why an allowlist rather than a denylist.
    """
    limits = get_limits()

    try:
        owner, repo = parsers.parse_github_url(body.url)
    except parsers.UnsupportedFile as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    count = len(
        (
            await db.execute(
                select(Document.id).where(Document.workspace_id == workspace.id)
            )
        ).all()
    )
    if count >= limits.max_documents_per_workspace:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This workspace already holds {limits.max_documents_per_workspace} documents.",
        )

    name = f"{owner}/{repo}"
    # The repo URL stands in for content here: the tarball is not downloaded
    # until the background task runs, so there are no bytes to hash yet.
    digest = hashlib.sha256(f"repo:{name}".encode()).hexdigest()

    existing = (
        await db.execute(
            select(Document).where(
                Document.workspace_id == workspace.id, Document.sha256 == digest
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That repository is already in this workspace."
        )

    document = Document(
        workspace_id=workspace.id,
        filename=name,
        mime="application/vnd.github.repository",
        sha256=digest,
        size_bytes=0,
        storage_key=f"{workspace.id}/repo-{uuid.uuid4().hex}",
        status=DocumentStatus.pending,
    )
    db.add(document)
    await db.flush()
    db.add(Job(document_id=document.id, user_id=workspace.owner_id, kind="ingest_repo"))
    await db.commit()

    background.add_task(_run_repo_ingestion, document.id, body.url)

    return {"id": str(document.id), "filename": name, "status": "pending"}


async def _run_ingestion(document_id: uuid.UUID, data: bytes) -> None:
    """Ingest outside the request, with its own session.

    The request's session is closed by the time this runs, so it opens a fresh
    one. Errors are already recorded on the document row by the pipeline; this
    only stops them from escaping into the worker's logs as unhandled.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            await pipeline.ingest_document(session, document_id, data)
        except Exception:
            log.exception("background ingestion failed for %s", document_id)


async def _run_repo_ingestion(document_id: uuid.UUID, url: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        try:
            await pipeline.ingest_repo(session, document_id, url)
        except Exception:
            log.exception("background repo ingestion failed for %s", document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
) -> None:
    document = await db.get(Document, document_id)
    if document is None or document.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    # Remove only this document's contribution; the rest of the graph stands.
    await queries.delete_document_subgraph(workspace.id, document.id)
    await db.delete(document)
    await db.commit()


# ------------------------------------------------------------------- graph --
@router.get("/graph")
async def get_graph(workspace: Workspace = Depends(require_workspace)) -> dict:
    """Nodes and edges for the canvas.

    Low-confidence edges are included rather than filtered: the editor draws
    them as dashed 'draft' so the user can confirm or delete them. Hiding them
    server-side would make them unfixable.
    """
    return await queries.get_graph(workspace.id, min_confidence=0.0)


@router.get("/node/{entity_id:path}")
async def get_node(
    entity_id: str, workspace: Workspace = Depends(require_workspace)
) -> dict:
    detail = await queries.get_node_detail(workspace.id, entity_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return detail


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = "concept"


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = None
    color: str | None = Field(default=None, max_length=9)
    notes: str | None = Field(default=None, max_length=20_000)
    user_edited: bool | None = None


@router.post("/node", status_code=status.HTTP_201_CREATED)
async def create_node(
    body: NodeCreate, workspace: Workspace = Depends(require_workspace)
) -> dict:
    # Any type is allowed, normalised to a stable key. The graph belongs to the
    # user; if their document is about pharmacology, "drug" is a better type
    # than forcing it into "concept".
    etype = normalize_type(body.type)
    entity_id = await queries.create_entity(workspace.id, body.name, etype)
    return {"id": entity_id, "name": body.name, "type": etype}


@router.patch("/node/{entity_id:path}")
async def patch_node(
    entity_id: str,
    body: NodePatch,
    workspace: Workspace = Depends(require_workspace),
) -> dict:

    # "Mark as correct" with no other change: inks the node and its edges.
    if body.user_edited and not any(
        v is not None for v in (body.name, body.type, body.color, body.notes)
    ):
        ok = await queries.confirm_entity(workspace.id, entity_id)
    else:
        ok = await queries.update_entity(
            workspace.id,
            entity_id,
            name=body.name,
            type=normalize_type(body.type) if body.type else None,
            color=body.color,
            notes=body.notes,
        )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"ok": True}


@router.delete("/node/{entity_id:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    entity_id: str, workspace: Workspace = Depends(require_workspace)
) -> None:
    if not await queries.delete_entity(workspace.id, entity_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


class EdgeBody(BaseModel):
    source: str
    target: str
    type: str
    color: str | None = Field(default=None, max_length=9)


@router.post("/edge", status_code=status.HTTP_201_CREATED)
async def create_edge(
    body: EdgeBody, workspace: Workspace = Depends(require_workspace)
) -> dict:
    if not await queries.upsert_edge(
        workspace.id, body.source, body.target, body.type, body.color
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One of those nodes is missing.")
    return {"ok": True}


@router.patch("/edge")
async def recolour_edge(
    body: EdgeBody, workspace: Workspace = Depends(require_workspace)
) -> dict:
    """Colour only. The link itself is not touched."""
    if not await queries.set_edge_color(
        workspace.id, body.source, body.target, body.type, body.color
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {"ok": True}


@router.delete("/edge", status_code=status.HTTP_204_NO_CONTENT)
async def remove_edge(
    body: EdgeBody, workspace: Workspace = Depends(require_workspace)
) -> None:
    if not await queries.delete_edge(
        workspace.id, body.source, body.target, body.type
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
