"""Endpoints REST: /api/companies/*."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.core import formatting
from app.core.cnpj_validator import normalize
from app.core.inbound_limit import check_company_lookup_limit
from app.schemas.company import CompanyUnified
from app.services import inscricao_estadual, web_research
from app.services.company_query import query_company_async
from app.services.report_context import build_report_context
from app.services.sanctions import check_sanctions
from app.web.pages import templates

router = APIRouter(prefix="/api/companies", tags=["companies"])

_REFRESH = Query(False, description="Ignora o cache e consulta as fontes novamente")
_LOOKUP_DEPS = [Depends(check_company_lookup_limit)]

_CNPJ_INVALID_DETAIL = (
    "CNPJ invalido: informe 14 caracteres (numeros ou letras A-Z) "
    "com digitos verificadores corretos."
)


async def _load(cnpj: str, atualizar: bool = False) -> CompanyUnified:
    try:
        return await query_company_async(cnpj, force_refresh=atualizar)
    except ValueError:
        raise HTTPException(status_code=400, detail=_CNPJ_INVALID_DETAIL) from None


@router.get("/{cnpj}", response_model=CompanyUnified, dependencies=_LOOKUP_DEPS)
async def get_company(cnpj: str, atualizar: bool = _REFRESH) -> CompanyUnified:
    """Consulta unificada do CNPJ (numerico ou alfanumerico) em todos os provedores habilitados."""
    return await _load(cnpj, atualizar)


@router.get("/{cnpj}/sources", dependencies=_LOOKUP_DEPS)
async def get_sources(cnpj: str) -> dict:
    """Tabela de procedencia isolada (sem dados da empresa)."""
    company = await _load(cnpj)
    return {
        "cnpj": company.cnpj,
        "fontes": [s.model_dump(mode="json") for s in company.fontes],
        "campos_procedencia": [p.model_dump(mode="json") for p in company.campos_procedencia],
        "conflitos": company.conflitos,
    }


@router.get("/{cnpj}/partners", dependencies=_LOOKUP_DEPS)
async def get_partners(cnpj: str) -> dict:
    """Quadro societario (apenas dados publicos)."""
    company = await _load(cnpj)
    return {"cnpj": company.cnpj, "socios": [s.model_dump(mode="json") for s in company.socios]}


def _download(content: str | bytes, media_type: str, filename: str) -> Response:
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{cnpj}/export.json", dependencies=_LOOKUP_DEPS)
async def export_json(cnpj: str) -> Response:
    """Exporta a consulta unificada em JSON para download."""
    company = await _load(cnpj)
    return _download(
        company.model_dump_json(indent=2), "application/json; charset=utf-8", f"cnpj_{company.cnpj}.json"
    )


@router.get("/{cnpj}/export.csv", dependencies=_LOOKUP_DEPS)
async def export_csv(
    cnpj: str,
    flatten: bool = Query(False, description="Se true, achata em uma unica linha"),
    excel: bool = Query(
        True, description="true: separador ';' e BOM UTF-8 (abre direto no Excel pt-BR); false: ',' sem BOM"
    ),
) -> Response:
    """Exporta a consulta unificada em CSV (somente dados publicos)."""
    company = await _load(cnpj)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";" if excel else ",")
    e = company.endereco
    base = [
        ("cnpj", company.cnpj),
        ("cnpj_formatado", company.cnpj_formatado),
        ("razao_social", company.razao_social),
        ("nome_fantasia", company.nome_fantasia),
        ("situacao_cadastral", company.situacao_cadastral),
        ("data_situacao_cadastral", company.data_situacao_cadastral),
        ("data_abertura", company.data_abertura),
        ("natureza_juridica", company.natureza_juridica),
        ("porte", company.porte),
        ("capital_social", company.capital_social),
        ("opcao_simples", company.opcao_simples),
        ("opcao_mei", company.opcao_mei),
        ("cnae_principal", company.cnae_principal.codigo if company.cnae_principal else None),
        ("logradouro", e.logradouro if e else None),
        ("numero", e.numero if e else None),
        ("complemento", e.complemento if e else None),
        ("bairro", e.bairro if e else None),
        ("municipio", e.municipio if e else None),
        ("uf", e.uf if e else None),
        ("cep", e.cep if e else None),
        ("email", company.email),
        ("matriz_filial", company.matriz_filial),
    ]

    cell = formatting.csv_cell  # neutraliza "=..." (formula injection no Excel)

    if flatten:
        writer.writerow([k for k, _ in base])
        writer.writerow([cell(v) for _, v in base])
    else:
        writer.writerow(["campo", "valor"])
        for k, v in base:
            writer.writerow([k, cell(v)])
        for c in company.cnaes_secundarios:
            writer.writerow(["cnae_secundario", c.codigo])
        for i, s in enumerate(company.socios, 1):
            writer.writerow([f"socio_{i}.nome", cell(s.nome)])
            writer.writerow([f"socio_{i}.qualificacao", cell(s.qualificacao)])
            writer.writerow([f"socio_{i}.documento_mascarado", cell(s.documento_mascarado)])
            writer.writerow([f"socio_{i}.fonte", cell(s.fonte)])
        for c in company.conflitos:
            writer.writerow(["conflito", cell(c)])
    text = buf.getvalue()
    content = ("\ufeff" + text).encode("utf-8") if excel else text
    return _download(content, "text/csv; charset=utf-8", f"cnpj_{company.cnpj}.csv")


@router.get("/{cnpj}/export.relatorio", response_class=HTMLResponse, dependencies=_LOOKUP_DEPS)
async def export_relatorio(
    request: Request,
    cnpj: str,
    escritorio: str = Query("", max_length=80),
    responsavel: str = Query("", max_length=80),
    referencia: str = Query("", max_length=80),
    cliente: str = Query("", max_length=80),
) -> HTMLResponse:
    """Relatorio de diligencia cadastral HTML (mesmo conteudo de /empresa/{cnpj}/relatorio)."""
    company = await _load(cnpj)
    ctx = build_report_context(
        company,
        sanctions=await check_sanctions(company.cnpj) if company.razao_social else None,
        escritorio=escritorio,
        responsavel=responsavel,
        referencia=referencia,
        cliente=cliente,
    )
    status = 200 if company.razao_social else 404
    return templates.TemplateResponse(request, "relatorio.html", ctx, status_code=status)


@router.get("/{cnpj}/research", dependencies=_LOOKUP_DEPS)
async def get_research_links(cnpj: str) -> dict:
    """Atalhos de pesquisa na internet (buscadores, reputacao, certidoes, mapas).

    Apenas monta links com dados da empresa; nada e enviado a terceiros.
    """
    company = await _load(cnpj)
    return {
        "cnpj": company.cnpj,
        "consulta_padrao": web_research.default_query(company),
        "busca_na_tela": {
            "habilitada": web_research.is_enabled(),
            "provedor": web_research.provider_label(),
        },
        "grupos": [g.to_dict() for g in web_research.build_research_links(company)],
    }


@router.get("/{cnpj}/sanctions", dependencies=_LOOKUP_DEPS)
async def get_sanctions(cnpj: str) -> dict:
    """Sancoes federais (CEIS/CNEP/CEPIM/CEAF) -- requer PORTAL_TRANSPARENCIA_API_KEY."""
    try:
        normalized = normalize(cnpj)
    except ValueError:
        raise HTTPException(status_code=400, detail=_CNPJ_INVALID_DETAIL) from None
    result = await check_sanctions(normalized)
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Consulta de sancoes desligada: defina PORTAL_TRANSPARENCIA_API_KEY no .env.",
        )
    return {
        "cnpj": normalized,
        "consultado": result.consultado,
        "sancionada": result.sancionada,
        "cadastros": result.cadastros,
        "erro": result.erro,
    }


@router.get("/{cnpj}/web-search", dependencies=_LOOKUP_DEPS)
async def get_web_search(
    cnpj: str,
    q: str | None = Query(None, max_length=300, description="Consulta; padrao: razao social + cidade"),
) -> dict:
    """Resultados de busca na web sobre a empresa (requer WEB_SEARCH_PROVIDER no .env)."""
    company = await _load(cnpj)
    query = (q or "").strip() or web_research.default_query(company)
    result = await web_research.search_web(query)
    return {"cnpj": company.cnpj} | result.to_dict()


@router.get("/{cnpj}/inscricao-estadual", dependencies=_LOOKUP_DEPS)
async def get_inscricao_estadual(
    cnpj: str,
    uf: str = Query(..., min_length=2, max_length=2, description="UF da SEFAZ a consultar"),
    atualizar: bool = _REFRESH,
) -> dict:
    """Inscricao estadual na SEFAZ da UF (requer CERTIFICADO_A1_PATH e CERTIFICADO_A1_SENHA)."""
    try:
        normalized = normalize(cnpj)
    except ValueError:
        raise HTTPException(status_code=400, detail=_CNPJ_INVALID_DETAIL) from None
    if not inscricao_estadual.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Consulta de inscricao estadual desligada: configure o certificado A1 no .env "
            "(CERTIFICADO_A1_PATH e CERTIFICADO_A1_SENHA) ou use o portal CCC.",
        )
    result = await inscricao_estadual.consultar_ie(normalized, uf, force_refresh=atualizar)
    return {"cnpj": normalized} | result.to_dict()
