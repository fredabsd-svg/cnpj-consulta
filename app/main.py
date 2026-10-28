"""Aplicacao FastAPI principal."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import admin, batch, busca, companies, health, history, partners, providers
from app.config import assert_bind_allowed, get_settings
from app.db import init_database
from app.providers.registry import get_registry
from app.receita.servico import agendador
from app.services.admin_auth import LoginNecessario
from app.services.official_docs import RECEITA_ORIGIN
from app.web.pages import router as pages_router
from app.web.pages_base import router as pages_base_router

STATIC_DIR = Path(__file__).parent / "static"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SECURITY_HEADERS = {
    # Sem scripts/estilos inline nem de terceiros: bloqueia XSS por injecao.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
        # Unico site externo que pode aparecer DENTRO do app: a pagina oficial do
        # Cartao CNPJ (o usuario resolve o captcha la; o app nao le o conteudo).
        f"frame-src {RECEITA_ORIGIN}"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin: nada vai para sites externos, e os formularios POST do
    # proprio app mantem o cabecalho Origin (com no-referrer o navegador manda
    # "Origin: null" e a protecao CSRF bloquearia o envio).
    "Referrer-Policy": "same-origin",
}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assert_bind_allowed(settings)  # fail-fast se host inseguro em production
    init_database()  # idempotente: cria as tabelas se faltarem
    logging.getLogger("uvicorn.access").addFilter(SemQueryString())
    # Atualizacao agendada da base (so age se ligada no painel Base de Dados CNPJ)
    tarefa = asyncio.create_task(agendador())
    yield
    tarefa.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tarefa
    await get_registry().aclose()


class SemQueryString(logging.Filter):
    """Tira a query string do log de acesso: buscas podem conter nomes de
    socios e outros dados pessoais (LGPD: logs sem dados desnecessarios)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0] + "?…", *args[3:])
        return True


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
    site = request.headers.get("sec-fetch-site")
    if site == "cross-site":
        return True
    if site in ("same-origin", "none"):
        return False
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


@app.exception_handler(LoginNecessario)
async def _login_necessario(request: Request, exc: LoginNecessario) -> Response:
    from urllib.parse import quote

    return RedirectResponse(f"/admin/entrar?proximo={quote(exc.destino)}", status_code=303)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(pages_router)
app.include_router(pages_base_router)
app.include_router(batch.router)
app.include_router(companies.router)
app.include_router(partners.router)
app.include_router(health.router)
app.include_router(providers.router)
app.include_router(history.router)
app.include_router(busca.router)
app.include_router(admin.router)
