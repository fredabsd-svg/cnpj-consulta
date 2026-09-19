"""Aplicacao FastAPI principal."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import companies, health, history, partners, providers
from app.config import get_settings
from app.db import init_database
from app.providers.registry import get_registry
from app.web.pages import router as pages_router

STATIC_DIR = Path(__file__).parent / "static"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SECURITY_HEADERS = {
    # Sem scripts/estilos inline nem de terceiros: bloqueia XSS por injecao.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_database()  # idempotente: cria as tabelas se faltarem
    yield
    await get_registry().aclose()


app = FastAPI(
    title="CNPJ Consulta",
    description="Consulta local de CNPJ e busca reversa por socios",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


def _is_cross_site(request: Request) -> bool:
    """Detecta requisicao de outra origem (protecao CSRF para app sem login)."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc != request.headers.get("host")
    return origin == "null"


@app.middleware("http")
async def security_middleware(request: Request, call_next) -> Response:
    if request.method not in _SAFE_METHODS and _is_cross_site(request):
        return PlainTextResponse("Requisicao de outra origem bloqueada.", status_code=403)
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        # /docs usa scripts de CDN; nao aplicamos CSP la.
        if key == "Content-Security-Policy" and request.url.path.startswith(("/docs", "/redoc")):
            continue
        response.headers.setdefault(key, value)
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(pages_router)
app.include_router(companies.router)
app.include_router(partners.router)
app.include_router(health.router)
app.include_router(providers.router)
app.include_router(history.router)
