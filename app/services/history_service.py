"""Servico de historico de consultas e favoritos."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select


def _utc_iso(value: datetime | None) -> str | None:
    """SQLite devolve datetime sem fuso; todos os registros sao gravados em UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def record_query(
    cnpj: str, razao_social: str | None, situacao: str | None, sources: str
) -> None:
    """Registra uma consulta no historico."""
    try:
        from app.db import get_session_factory
        from app.models.history import History
    except ImportError:
        return

    factory = get_session_factory()
    with factory() as s:
        row = History(
            cnpj=cnpj,
            razao_social=razao_social,
            situacao=situacao,
            sources_used=sources,
            queried_at=datetime.now(UTC),
        )
        s.add(row)
        s.commit()


def recent_queries(limit: int = 20, *, distinct: bool = False) -> list[dict[str, Any]]:
    """Retorna as ultimas consultas.

    distinct=True devolve so a consulta mais recente de cada CNPJ (atalhos
    da pagina inicial); o historico completo usa distinct=False.
    """
    try:
        from app.db import get_session_factory
        from app.models.history import History
    except ImportError:
        return []

    factory = get_session_factory()
    with factory() as s:
        query = select(History).order_by(History.queried_at.desc(), History.id.desc())
        if distinct:
            latest = select(func.max(History.id)).group_by(History.cnpj)
            query = query.where(History.id.in_(latest))
        rows = s.execute(query.limit(limit)).scalars().all()
        return [
            {
                "id": r.id,
                "cnpj": r.cnpj,
                "razao_social": r.razao_social,
                "situacao": r.situacao,
                "queried_at": _utc_iso(r.queried_at),
            }
            for r in rows
        ]


def clear_history() -> int:
    """Apaga todo o historico."""
    from sqlalchemy import delete

    from app.db import get_session_factory
    from app.models.history import History

    factory = get_session_factory()
    with factory() as s:
        result = s.execute(delete(History))
        s.commit()
        return result.rowcount or 0


def add_favorite(cnpj: str, label: str | None = None) -> None:
    from app.db import get_session_factory
    from app.models.favorite import Favorite

    factory = get_session_factory()
    with factory() as s:
        existing = s.execute(select(Favorite).where(Favorite.cnpj == cnpj)).scalar_one_or_none()
        if existing is not None:
            existing.label = label or existing.label
        else:
            s.add(Favorite(cnpj=cnpj, label=label))
        s.commit()


def remove_favorite(cnpj: str) -> bool:
    from sqlalchemy import delete

    from app.db import get_session_factory
    from app.models.favorite import Favorite

    factory = get_session_factory()
    with factory() as s:
        result = s.execute(delete(Favorite).where(Favorite.cnpj == cnpj))
        s.commit()
        return bool(result.rowcount)


def is_favorite(cnpj: str) -> bool:
    from app.db import get_session_factory
    from app.models.favorite import Favorite

    with get_session_factory()() as s:
        return s.execute(select(Favorite.id).where(Favorite.cnpj == cnpj)).first() is not None


def list_favorites() -> list[dict[str, Any]]:
    from app.db import get_session_factory
    from app.models.favorite import Favorite

    factory = get_session_factory()
    with factory() as s:
        rows = s.execute(select(Favorite).order_by(Favorite.created_at.desc())).scalars().all()
        return [
            {"cnpj": r.cnpj, "label": r.label, "created_at": _utc_iso(r.created_at)}
            for r in rows
        ]


def history_stats() -> dict[str, int]:
    """Numeros do painel inicial: consultas totais, de hoje e empresas distintas."""
    from app.db import get_session_factory
    from app.models.favorite import Favorite
    from app.models.history import History

    inicio_do_dia = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    # queried_at e gravado em UTC sem fuso (SQLite): compara na mesma base.
    corte = inicio_do_dia.astimezone(UTC).replace(tzinfo=None)
    with get_session_factory()() as s:
        total = s.scalar(select(func.count(History.id))) or 0
        hoje = s.scalar(select(func.count(History.id)).where(History.queried_at >= corte)) or 0
        empresas = s.scalar(select(func.count(func.distinct(History.cnpj)))) or 0
        favoritos = s.scalar(select(func.count(Favorite.id))) or 0
    return {"total": total, "hoje": hoje, "empresas": empresas, "favoritos": favoritos}
