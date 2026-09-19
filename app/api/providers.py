"""Endpoints REST: /api/providers/*."""

from __future__ import annotations

from fastapi import APIRouter

from app.providers.registry import get_registry

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("/status")
def status() -> dict:
    reg = get_registry()
    rows = []
    for p in reg.all():
        s = p.status
        rows.append(
            {
                "name": p.name,
                "enabled": p.enabled,
                "is_mirror_of_rfb": p.is_mirror_of_rfb,
                "base_url": p.base_url,
                "needs_api_key": p.needs_api_key,
                "rate_limit_per_minute": p.rate_limit_per_minute,
                "last_success": s.last_success.isoformat() if s.last_success else None,
                "last_failure": s.last_failure.isoformat() if s.last_failure else None,
                "consecutive_failures": s.consecutive_failures,
                "last_error": s.last_error,
                "total_queries": s.total_queries,
                "total_errors": s.total_errors,
            }
        )
    return {"providers": rows}
