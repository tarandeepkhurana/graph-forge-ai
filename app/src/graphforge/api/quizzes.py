"""Quizzes: a teacher generates one from a document, students take it once."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.ai import quiz as quiz_ai
from graphforge.api.deps import current_user, require_csrf, require_workspace
from graphforge.core.config import get_limits, get_settings
from graphforge.core.ratelimit import limiter
from graphforge.db.database import get_db
from graphforge.db.models import (
    AiUsage,
    Document,
    DocumentStatus,
    Quiz,
    QuizAttempt,
    User,
    Workspace,
)

workspace_router = APIRouter(prefix="/api/w/{workspace_id}", dependencies=[Depends(require_csrf)])
router = APIRouter(prefix="/api/quizzes", dependencies=[Depends(require_csrf)])

NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Quiz not found.")


class CreateQuiz(BaseModel):
    document_id: uuid.UUID
    num_questions: int = Field(default=10, ge=3, le=20)


class Attempt(BaseModel):
    answers: list[int] = Field(max_length=50)


@workspace_router.post("/quizzes")
async def create_quiz(
    body: CreateQuiz,
    workspace: Workspace = Depends(require_workspace),   # POST: owner or editor only
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not get_settings().openai_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI features are not configured.")

    document = (
        await db.execute(
            select(Document).where(
                Document.id == body.document_id, Document.workspace_id == workspace.id
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That document is not in this workspace.")
    if document.status != DocumentStatus.ready:
        raise HTTPException(status.HTTP_409_CONFLICT, "That document is still being processed.")

    await limiter.check(
        f"quiz:{user.id}", limit=get_limits().max_ai_calls_per_user_per_day, window_seconds=86_400
    )

    text = await quiz_ai.document_text(workspace.id, document.id)
    if not text.strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "No text was found in that document.")

    db.add(AiUsage(user_id=user.id, kind="quiz"))
    try:
        title, questions = await quiz_ai.generate(text, body.num_questions)
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not generate the quiz. Try again.")
    if not questions:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not generate the quiz. Try again.")

    quiz = Quiz(
        owner_id=user.id,
        workspace_id=workspace.id,
        document_id=document.id,
        document_name=document.filename,
        title=title,
        questions=questions,
    )
    db.add(quiz)
    await db.commit()
    return {"id": str(quiz.id), "title": quiz.title, "count": len(questions), "url": f"/q/{quiz.id}"}


@router.post("/{quiz_id}/attempt")
async def submit_attempt(
    quiz_id: uuid.UUID,
    body: Attempt,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    quiz = await db.get(Quiz, quiz_id)
    if quiz is None:
        raise NOT_FOUND
    if quiz.owner_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You created this quiz.")

    n = len(quiz.questions)
    if len(body.answers) != n or any(a < -1 or a > 3 for a in body.answers):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Answer every question once.")

    score = sum(1 for q, a in zip(quiz.questions, body.answers) if a == q["answer"])
    db.add(QuizAttempt(quiz_id=quiz.id, user_id=user.id, answers=body.answers, score=score, total=n))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already taken this quiz.")
    return {"score": score, "total": n}


@router.delete("/{quiz_id}")
async def delete_quiz(
    quiz_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    quiz = await db.get(Quiz, quiz_id)
    if quiz is None or quiz.owner_id != user.id:
        raise NOT_FOUND
    await db.delete(quiz)
    await db.commit()
    return {"ok": True}
