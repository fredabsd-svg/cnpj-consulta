"""Endpoints REST: /api/health."""

from __future__ import annotations

import platform
from datetime import UTC, datetime

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.app_env,
        "python": platform.python_version(),
        "receita_local": settings.receita_local_enabled,
        "timestamp": datetime.now(UTC).isoformat(),
    }
