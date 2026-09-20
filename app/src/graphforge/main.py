"""FastAPI application: one process serves the HTML and the JSON API.

Single-origin by design (decision D1). Because the pages and the API share an
origin there is no CORS to misconfigure, the session can live in an HttpOnly
cookie that JavaScript cannot read, and the CSP can be strict. That last point
is why there is no Alpine.js in this project: it needs `unsafe-eval`, and a CSP
with `unsafe-eval` is most of the way to no CSP at all.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from graphforge.core.config import get_settings
from graphforge.db.database import create_all
from graphforge.graph import client as graph_client

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "web" / "templates"
STATIC_DIR = HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Autoescaping is on by default in Jinja2Templates and must stay on: document
# text and model output both render through these templates, and both are
# untrusted.
templates.env.autoescape = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await create_all()
    await graph_client.init_schema()
    log.info("%s ready", settings.app_name)
    yield
    await graph_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        # No interactive docs in production: they enumerate every endpoint.
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # A per-request nonce lets our own inline <script> run while everything
        # injected into the page stays blocked.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        response.headers["Content-Security-Policy"] = "; ".join(
            [
                "default-src 'self'",
                f"script-src 'self' 'nonce-{nonce}'",
                "style-src 'self' 'unsafe-inline'",   # Cytoscape sets inline styles
                "img-src 'self' data:",
                "font-src 'self'",
                "connect-src 'self'",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "object-src 'none'",
            ]
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(500)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        # Stack traces and driver messages can echo query text and user data.
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            {"detail": "Something went wrong."}, status_code=500
        )

    from graphforge.api import auth, chat, workspaces
    from graphforge.web import views

    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(chat.router)
    app.include_router(views.router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
