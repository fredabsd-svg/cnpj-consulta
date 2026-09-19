"""Modelo de historico de consultas."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class History(Base):
    """Registro de cada consulta feita pelo usuario."""

    __tablename__ = "history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cnpj: Mapped[str] = mapped_column(String(14), index=True, nullable=False)
    razao_social: Mapped[str | None] = mapped_column(String(500), nullable=True)
    situacao: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sources_used: Mapped[str | None] = mapped_column(String(500), nullable=True)  # CSV
    queried_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
