"""Endpoints REST: /api/partners/*."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from app.core.cnpj_validator import normalize
from app.services.partner_search import MAX_LIMIT, companies_of_partner_document, search_partners

router = APIRouter(prefix="/api/partners", tags=["partners"])

_CNPJ_INVALID_DETAIL = (
    "CNPJ invalido: informe 14 caracteres (numeros ou letras A-Z) "
    "com digitos verificadores corretos."
)


@router.get("/search")
async def search(
    q: str = Query(..., min_length=3, max_length=200, description="Nome (ou parte do nome) do socio"),
    uf: str | None = Query(None, min_length=2, max_length=2, description="Sigla UF da sede"),
    municipio: str | None = Query(None, max_length=100, description="Municipio da sede"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
) -> dict:
    """Busca reversa por socio (requer base local habilitada)."""
    try:
        rows = await asyncio.to_thread(search_partners, q, uf=uf, municipio=municipio, limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    return {"total": len(rows), "results": rows}


@router.get("/{cnpj}/companies")
async def partner_companies(cnpj: str) -> dict:
    """Empresas em que o CNPJ informado (pessoa juridica) figura como socio.

    CPFs de pessoa fisica sao mascarados na base publica e nao podem ser
    usados como chave de busca.
    """
    try:
        normalized = normalize(cnpj)
    except ValueError:
        raise HTTPException(status_code=400, detail=_CNPJ_INVALID_DETAIL) from None
    try:
        rows = await asyncio.to_thread(companies_of_partner_document, normalized)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    return {"cnpj": normalized, "total": len(rows), "companies": rows}
