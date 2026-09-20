"""Chat endpoint: ask a question about the open document, streamed over SSE.

SSE rather than WebSocket because the traffic is one-way -- server to client --
and SSE rides the ordinary HTTP request that already carries our session cookie
and CSRF header. A WebSocket would need its own auth handshake for no gain.

The browser reads it with `fetch` and a stream reader, not `EventSource`:
`EventSource` cannot send custom headers, and every state-changing request here
must carry the CSRF token.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.ai import chat
from graphforge.api.deps import require_csrf, require_workspace
from graphforge.core.config import get_limits
from graphforge.core.ratelimit import limiter
from graphforge.db.database import get_db
from graphforge.db.models import AiUsage, Document, Workspace

router = APIRouter(prefix="/api/w/{workspace_id}", dependencies=[Depends(require_csrf)])


class Question(BaseModel):
    document_id: uuid.UUID
    question: str = Field(min_length=1, max_length=2000)


@router.post("/chat")
async def ask(
    body: Question,
    request: Request,
    workspace: Workspace = Depends(require_workspace),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    limits = get_limits()

    # An attacker who cannot read your data can still empty your API account,
    # so this cap is a security control rather than a billing feature.
    await limiter.check(
        f"chat:{workspace.owner_id}",
        limit=limits.max_ai_calls_per_user_per_day,
        window_seconds=86_400,
    )

    # The document must belong to this workspace. Checked here so a mismatched
    # id cannot be used to read across workspaces through the chat endpoint.
    document = (
        await db.execute(
            select(Document).where(
                Document.id == body.document_id,
                Document.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()

    if document is None:
        async def not_found():
            yield chat.to_sse(
                {"type": "error", "message": "That document is not in this workspace."}
            )

        return StreamingResponse(not_found(), media_type="text/event-stream")

    db.add(AiUsage(user_id=workspace.owner_id, kind="chat"))
    await db.commit()

    async def stream():
        async for event in chat.answer(workspace.id, document.id, body.question):
            yield chat.to_sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx and friends not to buffer, which would defeat streaming.
            "X-Accel-Buffering": "no",
        },
    )
