"""Cache de respostas unificadas por CNPJ (TTL configuravel)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import get_session_factory
from app.models.cached_query import CachedQuery
from app.schemas.company import CompanyUnified

log = logging.getLogger(__name__)

# Suba a versao sempre que a conciliacao mudar de forma que invalide
# respostas antigas: entradas de outras versoes sao simplesmente ignoradas.
# v2: correcao de capital social, conflitos e socios (app 0.2.0).
# v3: procedencia/conflitos para endereco, telefones e CNAEs.
CACHE_VERSION = 3
_UNIFIED = f"unified-v{CACHE_VERSION}"


def _now() -> datetime:
    return datetime.now(UTC)


def cache_get(cnpj: str) -> CompanyUnified | None:
    """Busca no cache. Devolve None se ausente, expirado ou corrompido."""
    settings = get_settings()
    ttl = settings.cache_ttl_seconds
    if settings.cache_backend == "memory":
        return _memory_cache_get(cnpj, ttl)
    return _sqlite_cache_get(cnpj, ttl)


def cache_put(cnpj: str, data: CompanyUnified) -> None:
    if get_settings().cache_backend == "memory":
        _memory_cache_put(cnpj, data)
    else:
        _sqlite_cache_put(cnpj, data)


def clear_cache() -> int:
    if get_settings().cache_backend == "memory":
        return _memory_cache_clear()
    return _sqlite_cache_clear()


# ---------- Memory backend ----------

_memory: dict[str, tuple[datetime, str]] = {}


def _memory_cache_get(cnpj: str, ttl: int) -> CompanyUnified | None:
    item = _memory.get(cnpj)  # memoria nao sobrevive a reinicio: sem versao
    if item is None:
        return None
    when, payload = item
    if (_now() - when).total_seconds() > ttl:
        _memory.pop(cnpj, None)
        return None
    try:
        return CompanyUnified.model_validate_json(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("cache corrompido para %s: %s", cnpj, e)
        _memory.pop(cnpj, None)
        return None


def _memory_cache_put(cnpj: str, data: CompanyUnified) -> None:
    _memory[cnpj] = (_now(), data.model_dump_json())


def _memory_cache_clear() -> int:
    n = len(_memory)
    _memory.clear()
    return n


# ---------- SQLite backend ----------
# fetched_at e gravado como UTC "ingenuo" (sem tzinfo), igual ao
# CURRENT_TIMESTAMP do SQLite usado como server_default.


def _sqlite_cache_get(cnpj: str, ttl: int) -> CompanyUnified | None:
    with get_session_factory()() as s:
        row = s.execute(
            select(CachedQuery).where(CachedQuery.cnpj == cnpj, CachedQuery.provider == _UNIFIED)
        ).scalar_one_or_none()
        if row is None or row.response_json is None:
            return None
        if (_now() - row.fetched_at.replace(tzinfo=UTC)).total_seconds() > ttl:
            return None
        try:
            return CompanyUnified.model_validate_json(row.response_json)
        except Exception as e:  # noqa: BLE001
            log.warning("cache corrompido para %s: %s", cnpj, e)
            return None


def _sqlite_cache_put(cnpj: str, data: CompanyUnified) -> None:
    """Insere ou ATUALIZA a linha (cnpj, 'unified') -- ha restricao de unicidade."""
    payload = data.model_dump_json()
    now = _now().replace(tzinfo=None)
    with get_session_factory()() as s:
        row = s.execute(
            select(CachedQuery).where(CachedQuery.cnpj == cnpj, CachedQuery.provider == _UNIFIED)
        ).scalar_one_or_none()
        if row is None:
            s.add(
                CachedQuery(
                    cnpj=cnpj, provider=_UNIFIED, status=200, response_json=payload, fetched_at=now
                )
            )
        else:
            row.status = 200
            row.response_json = payload
            row.error = None
            row.fetched_at = now
        s.commit()


def _sqlite_cache_clear() -> int:
    with get_session_factory()() as s:
        result = s.execute(delete(CachedQuery))
        s.commit()
        return result.rowcount or 0
