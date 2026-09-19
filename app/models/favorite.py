"""Modelo de CNPJs favoritos."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Favorite(Base):
    """CNPJ favoritado pelo usuario."""

    __tablename__ = "favorite"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cnpj: Mapped[str] = mapped_column(String(14), unique=True, index=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
