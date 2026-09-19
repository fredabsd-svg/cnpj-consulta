"""Cache de respostas por provedor."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CachedQuery(Base):
    """Resposta cacheada de um provedor para um CNPJ.

    TTL controlado por `cache_ttl_seconds` do config.
    """

    __tablename__ = "cached_query"
    __table_args__ = (UniqueConstraint("cnpj", "provider", name="uq_cnpj_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cnpj: Mapped[str] = mapped_column(String(14), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # 200, 429, 500, etc.
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
