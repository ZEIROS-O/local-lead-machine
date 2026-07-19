from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .data import DashboardRepository, default_repo_root


class SecurityHeadersMiddleware:
    """Add browser hardening headers without BaseHTTPMiddleware buffering."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self'; img-src 'self' data:; connect-src 'self'"
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _static_response(path: Path, *, immutable: bool = False) -> Response:
    media_type, _ = mimetypes.guess_type(path.name)
    return Response(
        content=path.read_bytes(),
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"},
    )


def create_app(root: Path | str | None = None, *, frontend_dist: Path | str | None = None) -> FastAPI:
    repo = DashboardRepository(root or default_repo_root())
    dist = Path(frontend_dist) if frontend_dist else repo.root / "dashboard-ui" / "dist"
    app = FastAPI(title="Local Lead Machine Dashboard", version="1.0.0")
    app.state.dashboard_repository = repo

    app.add_middleware(SecurityHeadersMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    async def health() -> dict:
        return repo.health()

    @app.get("/api/overview")
    async def overview(vertical: str = "", market: str = "", refresh: bool = False) -> dict:
        return repo.overview(vertical=vertical, market=market, force=refresh)

    @app.get("/api/batches")
    async def batches(
        vertical: str = "",
        market: str = "",
        progress: str = "",
        health: str = "",
        q: str = "",
        unscored: bool | None = None,
        finished: bool | None = None,
        refresh: bool = False,
    ) -> dict:
        return repo.batches(
            vertical=vertical,
            market=market,
            progress=progress,
            health=health,
            query=q,
            unscored=unscored,
            finished=finished,
            force=refresh,
        )

    @app.get("/api/batches/{batch}")
    async def batch_detail(batch: str, refresh: bool = False) -> dict:
        result = repo.batch(batch, force=refresh)
        if result is None:
            raise HTTPException(status_code=404, detail="Canonical batch not found")
        return result

    @app.get("/api/batches/{batch}/leads")
    async def batch_leads(
        batch: str,
        state: Literal["all", "scored", "unscored"] = "all",
        priority: str = "",
        tier: str = "",
        sendable: bool | None = None,
        money_signal: bool | None = None,
        q: str = "",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        refresh: bool = False,
    ) -> dict:
        result = repo.leads(
            batch,
            state=state,
            priority=priority,
            tier=tier,
            sendable=sendable,
            money_signal=money_signal,
            query=q,
            page=page,
            page_size=page_size,
            force=refresh,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Canonical batch not found")
        return result

    if dist.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str) -> Response:
            if path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return _static_response(candidate, immutable=path.startswith("assets/"))
            return _static_response(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "Dashboard API is running. Build dashboard-ui to serve the frontend.",
                    "api": "/api/overview",
                }
            )

    return app


app = create_app()
