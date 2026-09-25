"""Rotas HTML (Jinja2, renderizadas no servidor; JS apenas para melhorias).

Todas as paginas funcionam sem JavaScript: formularios usam GET simples e
os erros sao renderizados no proprio formulario (nunca em texto cru).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import get_settings
from app.core import formatting
from app.core.cnpj_validator import normalize_or_none, strip
from app.core.inbound_limit import check_company_lookup_limit, reserver_for
from app.providers.registry import get_registry
from app.services import inscricao_estadual, official_docs, web_research
from app.services.batch import parse_cnpj_list, run_batch, summarize
from app.services.company_query import query_company_async
from app.services.diligence import STATUS_LABELS, build_diligence_checklist, build_diligence_verdict
from app.services.history_service import history_stats, is_favorite, list_favorites, recent_queries
from app.services.partner_search import MAX_LIMIT, search_partners
from app.services.report_context import build_report_context
from app.services.sanctions import check_sanctions

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
for _name in ("brl", "data_br", "cep", "telefone", "cnae", "cnpj", "fonte", "fontes_no_texto", "idade"):
    templates.env.filters[_name] = getattr(formatting, _name)

UFS = (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)


async def _none() -> None:
    return None


def _render(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    ctx.setdefault("active", "")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


async def _index(request: Request, *, error: str | None = None, value: str = "", status: int = 200):
    recent, favorites, stats = await asyncio.gather(
        asyncio.to_thread(recent_queries, 8, distinct=True),
        asyncio.to_thread(list_favorites),
        asyncio.to_thread(history_stats),
    )
    return _render(
        request,
        "index.html",
        {
            "active": "inicio",
            "error": error,
            "value": value,
            "recent": recent,
            "favorites": favorites,
            "stats": stats,
            "fontes_ativas": [p.name for p in get_registry().all()],
            "receita_local": get_settings().receita_local_enabled,
        },
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
            error="CNPJ inválido. Confira os 14 caracteres e os 2 dígitos verificadores.",
            value=cnpj[:18],
            status=400,
        )
    return RedirectResponse(f"/empresa/{normalized}", status_code=303)


@router.get(
    "/empresa/{cnpj}",
    response_class=HTMLResponse,
    dependencies=[Depends(check_company_lookup_limit)],
)
async def empresa(request: Request, cnpj: str, atualizar: bool = False) -> HTMLResponse:
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ inválido. Confira os dígitos.", value=cnpj[:18], status=400
        )
    company = await query_company_async(normalized, force_refresh=atualizar)
    favorite, sanctions = await asyncio.gather(
        asyncio.to_thread(is_favorite, normalized),
        check_sanctions(normalized, force_refresh=atualizar) if company.razao_social else _none(),
    )
    diligence = build_diligence_checklist(company, sanctions) if company.razao_social else []
    return _render(
        request,
        "company.html",
        {
            "active": "inicio",
            "company": company,
            "favorite": favorite,
            "diligence": diligence,
            "verdict": build_diligence_verdict(diligence) if diligence else None,
            "status_labels": STATUS_LABELS,
            **_research_ctx(company),
        },
        200 if company.razao_social else 404,
    )


def _research_ctx(company, query: str | None = None) -> dict:
    return {
        "research_groups": web_research.build_research_links(company) if company.razao_social else [],
        "web_query": query or web_research.default_query(company),
        "web_enabled": web_research.is_enabled(),
        "web_provider": web_research.provider_label(),
    }


@router.get(
    "/empresa/{cnpj}/internet",
    response_class=HTMLResponse,
    dependencies=[Depends(check_company_lookup_limit)],
)
async def empresa_internet(
    request: Request, cnpj: str, q: str = Query("", max_length=300)
) -> HTMLResponse:
    """Pesquisa na internet sobre a empresa (versao sem JavaScript da aba)."""
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ inválido. Confira os dígitos.", value=cnpj[:18], status=400
        )
    company = await query_company_async(normalized)
    ctx = {"active": "inicio", "company": company, **_research_ctx(company, q.strip() or None)}
    busca = ctx["web_enabled"] and company.razao_social
    ctx["web"] = await web_research.search_web(ctx["web_query"]) if busca else None
    return _render(request, "internet.html", ctx, 200 if company.razao_social else 404)


@router.get(
    "/empresa/{cnpj}/relatorio",
    response_class=HTMLResponse,
    dependencies=[Depends(check_company_lookup_limit)],
)
async def empresa_relatorio(
    request: Request,
    cnpj: str,
    atualizar: bool = False,
    escritorio: str = Query("", max_length=80),
    responsavel: str = Query("", max_length=80),
    referencia: str = Query("", max_length=80),
    cliente: str = Query("", max_length=80),
) -> HTMLResponse:
    """Relatorio de diligencia cadastral (HTML A4 / PDF via impressao do navegador)."""
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ inválido. Confira os dígitos.", value=cnpj[:18], status=400
        )
    company = await query_company_async(normalized, force_refresh=atualizar)
    ctx = build_report_context(
        company,
        sanctions=(
            await check_sanctions(normalized, force_refresh=atualizar) if company.razao_social else None
        ),
        escritorio=escritorio,
        responsavel=responsavel,
        referencia=referencia,
        cliente=cliente,
    )
    ctx["active"] = "inicio"
    return _render(
        request,
        "relatorio.html",
        ctx,
        200 if company.razao_social else 404,
    )


@router.get(
    "/empresa/{cnpj}/cartao-cnpj",
    response_class=HTMLResponse,
    dependencies=[Depends(check_company_lookup_limit)],
)
async def empresa_cartao_cnpj(request: Request, cnpj: str) -> HTMLResponse:
    """Cartao CNPJ oficial: o site da Receita dentro do app, com o CNPJ preenchido.

    O app nao emite o documento: o usuario resolve o captcha e imprime no
    proprio site da Receita (exibido num quadro, ou em nova janela).
    """
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ inválido. Confira os dígitos.", value=cnpj[:18], status=400
        )
    # So para o cabecalho (quase sempre vem do cache); a pagina funciona mesmo
    # se nenhuma fonte responder, pois quem emite o cartao e a Receita.
    company = await query_company_async(normalized)
    return _render(
        request,
        "cartao_cnpj.html",
        {
            "active": "inicio",
            "company": company,
            "oficial_url": official_docs.comprovante_url(normalized),
            "oficial_origem": official_docs.RECEITA_ORIGIN.removeprefix("https://"),
        },
    )


@router.get(
    "/empresa/{cnpj}/inscricao-estadual",
    response_class=HTMLResponse,
    dependencies=[Depends(check_company_lookup_limit)],
)
async def empresa_inscricao_estadual(
    request: Request,
    cnpj: str,
    uf: str = Query("", max_length=2),
    atualizar: bool = False,
) -> HTMLResponse:
    """Inscricao estadual: consulta automatica na SEFAZ (certificado A1) ou portal CCC."""
    normalized = normalize_or_none(cnpj)
    if normalized is None:
        return await _index(
            request, error="CNPJ inválido. Confira os dígitos.", value=cnpj[:18], status=400
        )
    company = await query_company_async(normalized)
    uf_empresa = (company.endereco.uf or "").upper() if company.endereco else ""
    uf_sel = uf.strip().upper() or uf_empresa
    consulta = None
    if inscricao_estadual.is_enabled() and uf_sel:
        consulta = await inscricao_estadual.consultar_ie(normalized, uf_sel, force_refresh=atualizar)
    return _render(
        request,
        "inscricao_estadual.html",
        {
            "active": "inicio",
            "company": company,
            "ufs": UFS,
            "uf_sel": uf_sel,
            "uf_empresa": uf_empresa,
            "ws_enabled": inscricao_estadual.is_enabled(),
            "ws_ufs": sorted(inscricao_estadual.ENDPOINTS),
            "consulta": consulta,
            "ccc_url": official_docs.CCC_PORTAL_URL,
            "sintegra_url": official_docs.SINTEGRA_URL,
        },
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


@router.get("/lote", response_class=HTMLResponse)
async def lote(request: Request, cnpjs: str = Query("", max_length=4000)) -> HTMLResponse:
    """Consulta em lote: cola uma lista de CNPJs e recebe um quadro-resumo."""
    settings = get_settings()
    ctx: dict = {
        "active": "lote",
        "cnpjs": cnpjs,
        "maximo": settings.batch_max_items,
        "parsed": None,
        "rows": None,
        "resumo": None,
        "error": None,
    }
    status = 200
    if cnpjs.strip():
        parsed = parse_cnpj_list(cnpjs, settings.batch_max_items)
        ctx["parsed"] = parsed
        if not parsed.validos:
            ctx["error"], status = "Nenhum CNPJ válido na lista. Confira os dígitos verificadores.", 400
        else:
            rows = await run_batch(parsed.validos, reserve=reserver_for(request))
            ctx["rows"], ctx["resumo"] = rows, summarize(rows)
            ctx["cnpjs_normalizados"] = ",".join(parsed.validos)
    return _render(request, "batch.html", ctx, status)


@router.get("/fontes", response_class=HTMLResponse)
async def fontes(request: Request) -> HTMLResponse:
    """Status das fontes: saude, limites e o que esta ligado no .env."""
    settings = get_settings()
    reg = get_registry()
    return _render(
        request,
        "status.html",
        {
            "active": "fontes",
            "ativas": reg.all(),
            "desligadas": reg.disabled(),
            "settings": settings,
            "web_enabled": web_research.is_enabled(settings),
            "web_provider": web_research.provider_label(settings),
        },
    )
