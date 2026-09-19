"""Rotas HTML (Jinja2, renderizadas no servidor; JS apenas para melhorias).

Todas as paginas funcionam sem JavaScript: formularios usam GET simples e
os erros sao renderizados no proprio formulario (nunca em texto cru).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import get_settings
from app.core import formatting
from app.core.cnpj_validator import normalize_or_none, strip
from app.services.company_query import query_company_async
from app.services.history_service import is_favorite, list_favorites, recent_queries
from app.services.partner_search import MAX_LIMIT, search_partners

router = APIRouter(tags=["web"], include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__


def static_url(path: str) -> str:
    """URL de arquivo estatico com versao pela data de modificacao.

    Assim o navegador baixa o CSS/JS novo assim que o arquivo muda, sem
    depender de alguem lembrar de subir a versao do app.
    """
    try:
        stamp = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{path}?v={stamp}"


templates.env.globals["static_url"] = static_url
for _name in ("brl", "data_br", "cep", "telefone", "cnae", "cnpj", "fonte"):
    templates.env.filters[_name] = getattr(formatting, _name)

UFS = (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)


def _render(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    ctx.setdefault("active", "")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


async def _index(request: Request, *, error: str | None = None, value: str = "", status: int = 200):
    recent, favorites = await asyncio.gather(
        asyncio.to_thread(recent_queries, 8, distinct=True), asyncio.to_thread(list_favorites)
    )
    return _render(
        request,
        "index.html",
        {"active": "inicio", "error": error, "value": value, "recent": recent, "favorites": favorites},
        status,
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return await _index(request)


@router.get("/consulta")
async def consulta(request: Request, cnpj: str = "") -> Response:
    """Destino do formulario da pagina inicial: valida e redireciona."""
    if not strip(cnpj):
        return await _index(request, error="Informe um CNPJ.", status=400)
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request,
            error="CNPJ invalido. Confira os 14 caracteres e os 2 digitos verificadores.",
            value=cnpj[:18],
            status=400,
        )
    return RedirectResponse(f"/empresa/{normalized}", status_code=303)


@router.get("/empresa/{cnpj}", response_class=HTMLResponse)
async def empresa(request: Request, cnpj: str, atualizar: bool = False) -> HTMLResponse:
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ invalido. Confira os digitos.", value=cnpj[:18], status=400
        )
    company = await query_company_async(normalized, force_refresh=atualizar)
    favorite = await asyncio.to_thread(is_favorite, normalized)
    return _render(
        request,
        "company.html",
        {"active": "inicio", "company": company, "favorite": favorite},
        200 if company.razao_social else 404,
    )


@router.get("/socios", response_class=HTMLResponse)
async def socios(
    request: Request,
    q: str = Query("", max_length=200),
    uf: str = Query("", max_length=2),
    municipio: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
) -> HTMLResponse:
    settings = get_settings()
    ctx: dict = {
        "active": "socios",
        "enabled": settings.receita_local_enabled,
        "q": q.strip(),
        "uf": uf.strip().upper(),
        "municipio": municipio.strip(),
        "ufs": UFS,
        "rows": None,
        "error": None,
    }
    status = 200
    if ctx["q"]:
        if len(ctx["q"]) < 3:
            ctx["error"], status = "Digite ao menos 3 letras do nome.", 400
        else:
            try:
                ctx["rows"] = await asyncio.to_thread(
                    search_partners,
                    ctx["q"],
                    uf=ctx["uf"] or None,
                    municipio=ctx["municipio"] or None,
                    limit=limit,
                )
                ctx["limit"] = limit
            except RuntimeError as e:
                ctx["error"], status = str(e), 503
    return _render(request, "partner_search.html", ctx, status)


@router.get("/historico", response_class=HTMLResponse)
async def historico(request: Request) -> HTMLResponse:
    rows, favs = await asyncio.gather(
        asyncio.to_thread(recent_queries, 100), asyncio.to_thread(list_favorites)
    )
    return _render(request, "history.html", {"active": "historico", "queries": rows, "favorites": favs})


@router.get("/privacidade", response_class=HTMLResponse)
async def privacidade(request: Request) -> HTMLResponse:
    settings = get_settings()
    return _render(
        request,
        "privacy.html",
        {
            "active": "privacidade",
            "contact_email": settings.app_contact_email,
            "cache_hours": settings.cache_ttl_seconds / 3600,
        },
    )
