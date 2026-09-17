from __future__ import annotations

import logging
import secrets
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_all
from .security import same_origin_request
from .routers import auth_router, chat_router, health_router, pages_router, sources_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_all()
    yield


app = FastAPI(title="ETPOS Assistant", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or secrets.token_hex(12)
    if not same_origin_request(request):
        response = PlainTextResponse("Requête cross-origin refusée.", status_code=403)
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    )
    return response


app.include_router(health_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(sources_router)
app.include_router(pages_router)
