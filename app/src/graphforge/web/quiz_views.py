"""Quiz pages: the teacher's list and results, and the student's attempt page."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from graphforge.api.deps import current_user_optional
from graphforge.core.config import get_settings
from graphforge.db.database import get_db
from graphforge.db.models import Quiz, QuizAttempt, User

router = APIRouter(include_in_schema=False)

# Where to return after sign-in. Only quiz paths, so it cannot be an open redirect.
NEXT_COOKIE = "gf_next"


def is_safe_next(path: str | None) -> bool:
    return bool(path) and (path.startswith("/q/") or path.startswith("/quizzes")) and "//" not in path


def _to_signin(request: Request) -> RedirectResponse:
    response = RedirectResponse("/signin", status_code=303)
    response.set_cookie(
        NEXT_COOKIE,
        request.url.path,
        max_age=1800,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )
    return response


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    from graphforge.main import templates

    return templates.TemplateResponse(
        request, name, {**ctx, "csp_nonce": getattr(request.state, "csp_nonce", "")}
    )


@router.get("/quizzes", response_class=HTMLResponse)
async def my_quizzes(
    request: Request,
    user: User | None = Depends(current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        return _to_signin(request)

    created = (
        await db.execute(
            select(Quiz, func.count(QuizAttempt.id))
            .outerjoin(QuizAttempt, QuizAttempt.quiz_id == Quiz.id)
            .where(Quiz.owner_id == user.id)
            .group_by(Quiz.id)
            .order_by(Quiz.created_at.desc())
        )
    ).all()
    taken = (
        await db.execute(
            select(QuizAttempt, Quiz)
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .where(QuizAttempt.user_id == user.id)
            .order_by(QuizAttempt.created_at.desc())
        )
    ).all()
    return _render(request, "quizzes.html", {"user": user, "created": created, "taken": taken})


@router.get("/quizzes/{quiz_id}", response_class=HTMLResponse)
async def quiz_results(
    quiz_id: uuid.UUID,
    request: Request,
    user: User | None = Depends(current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        return _to_signin(request)
    quiz = await db.get(Quiz, quiz_id)
    if quiz is None or quiz.owner_id != user.id:
        raise HTTPException(404, "Quiz not found.")

    attempts = (
        await db.execute(
            select(QuizAttempt, User.email)
            .join(User, User.id == QuizAttempt.user_id)
            .where(QuizAttempt.quiz_id == quiz.id)
            .order_by(QuizAttempt.created_at)
        )
    ).all()
    return _render(request, "quiz_results.html", {"quiz": quiz, "attempts": attempts})


@router.get("/q/{quiz_id}", response_class=HTMLResponse)
async def take_quiz(
    quiz_id: uuid.UUID,
    request: Request,
    user: User | None = Depends(current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        return _to_signin(request)
    quiz = await db.get(Quiz, quiz_id)
    if quiz is None:
        raise HTTPException(404, "Quiz not found.")
    if quiz.owner_id == user.id:
        return RedirectResponse(f"/quizzes/{quiz.id}", status_code=303)

    attempt = (
        await db.execute(
            select(QuizAttempt).where(QuizAttempt.quiz_id == quiz.id, QuizAttempt.user_id == user.id)
        )
    ).scalar_one_or_none()
    return _render(request, "quiz_take.html", {"quiz": quiz, "attempt": attempt, "user": user})
