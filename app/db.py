"""Configuracao do SQLAlchemy: engine, session, Base."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
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
        is_sqlite = url.startswith("sqlite")
        # timeout: o processo de atualizacao da base escreve progresso ao mesmo
        # tempo que o servidor le; WAL permite leitura durante a escrita.
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        _engine = create_engine(url, connect_args=connect_args, echo=False, future=True)
        if is_sqlite:
            event.listen(_engine, "connect", _sqlite_pragmas)
    return _engine


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        if ":memory:" not in str(get_settings().database_url):
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def init_database() -> None:
    """Cria todas as tabelas (apenas para SQLite principal)."""
    from app.models import (
        base_cnpj,  # noqa: F401
        cached_query,  # noqa: F401
        chat,  # noqa: F401
        favorite,  # noqa: F401
        history,  # noqa: F401 -- registrar modelos
    )

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _migrar_chat_autoincrement(engine)


def _migrar_chat_autoincrement(engine) -> None:
    """chat_conversa criada antes da v0.5 nao tem AUTOINCREMENT (id de conversa
    apagada era reaproveitado). Recria a tabela uma vez, preservando os dados."""
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_conversa'")).scalar()
        if not ddl or "AUTOINCREMENT" in ddl.upper():
            return
        conn.execute(text("ALTER TABLE chat_conversa RENAME TO _chat_conversa_antiga"))
        # Os indices mantem o nome ao renomear a tabela: liberam o nome para a nova.
        for (idx,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index' "
                                        "AND tbl_name='_chat_conversa_antiga' AND sql IS NOT NULL")).all():
            conn.execute(text(f'DROP INDEX "{idx}"'))
        from app.models.chat import ChatConversa

        ChatConversa.__table__.create(bind=conn)
        cols = ", ".join(c.name for c in ChatConversa.__table__.columns)
        conn.execute(text(f"INSERT INTO chat_conversa ({cols}) SELECT {cols} FROM _chat_conversa_antiga"))
        conn.execute(text("DROP TABLE _chat_conversa_antiga"))


def get_session() -> Iterator[Session]:
    """Dependencia FastAPI / context manager."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
