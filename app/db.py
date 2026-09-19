"""Configuracao do SQLAlchemy: engine, session, Base."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Base comum para todos os modelos."""


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_data_dir(url: str) -> None:
    """Cria diretorio do SQLite se necessario."""
    if not url.startswith("sqlite:///"):
        return
    path = url.removeprefix("sqlite:///")
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        _ensure_data_dir(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, echo=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def init_database() -> None:
    """Cria todas as tabelas (apenas para SQLite principal)."""
    from app.models import (
        cached_query,  # noqa: F401
        favorite,  # noqa: F401
        history,  # noqa: F401 -- registrar modelos
    )

    Base.metadata.create_all(bind=get_engine())


def get_session() -> Iterator[Session]:
    """Dependencia FastAPI / context manager."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
