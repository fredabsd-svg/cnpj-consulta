"""Endpoints REST: /api/companies/*."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.core.inbound_limit import check_company_lookup_limit
from app.schemas.company import CompanyUnified
from app.services.company_query import query_company_async

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

    def cell(v: object) -> object:
        if v is None:
            return ""
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

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
            writer.writerow([f"socio_{i}.nome", s.nome or ""])
            writer.writerow([f"socio_{i}.qualificacao", s.qualificacao or ""])
            writer.writerow([f"socio_{i}.documento_mascarado", s.documento_mascarado or ""])
            writer.writerow([f"socio_{i}.fonte", s.fonte])
        for c in company.conflitos:
            writer.writerow(["conflito", c])
    text = buf.getvalue()
    content = ("\ufeff" + text).encode("utf-8") if excel else text
    return _download(content, "text/csv; charset=utf-8", f"cnpj_{company.cnpj}.csv")
