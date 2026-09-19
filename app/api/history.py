"""Endpoints REST: /api/history e /api/favorites."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.cnpj_validator import normalize
from app.services.history_service import (
    add_favorite,
    clear_history,
    list_favorites,
    recent_queries,
    remove_favorite,
)

router = APIRouter(prefix="/api", tags=["history"])


def _valid_cnpj(cnpj: str) -> str:
    try:
        return normalize(cnpj)
    except ValueError:
        raise HTTPException(status_code=400, detail="CNPJ invalido") from None


@router.get("/history")
def history(limit: int = Query(50, ge=1, le=500)) -> dict:
    rows = recent_queries(limit=limit)
    return {"total": len(rows), "queries": rows}


@router.delete("/history")
def delete_history() -> dict:
    return {"removed": clear_history()}


@router.get("/favorites")
def get_favorites() -> dict:
    return {"favorites": list_favorites()}


@router.post("/favorites/{cnpj}")
def post_favorite(cnpj: str, label: str | None = Query(None, max_length=200)) -> dict:
    n = _valid_cnpj(cnpj)
    add_favorite(n, label)
    return {"cnpj": n, "label": label, "favorite": True}


@router.delete("/favorites/{cnpj}")
def delete_favorite(cnpj: str) -> dict:
    n = _valid_cnpj(cnpj)
    return {"cnpj": n, "removed": remove_favorite(n), "favorite": False}
