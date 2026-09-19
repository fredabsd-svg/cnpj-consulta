"""Testes do cache SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db import get_session_factory
from app.models.cached_query import CachedQuery
from app.schemas.company import CompanyUnified
from app.services.cache import cache_get, cache_put, clear_cache


def _company(nome: str) -> CompanyUnified:
    return CompanyUnified(cnpj="19131243000197", cnpj_formatado="19.131.243/0001-97", razao_social=nome)


def _age_rows(days: int) -> None:
    with get_session_factory()() as s:
        for row in s.query(CachedQuery).all():
            row.fetched_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        s.commit()


def test_roundtrip(db_initialized):
    cache_put("19131243000197", _company("A"))
    assert cache_get("19131243000197").razao_social == "A"


def test_put_twice_updates_same_row(db_initialized):
    cache_put("19131243000197", _company("A"))
    cache_put("19131243000197", _company("B"))
    assert cache_get("19131243000197").razao_social == "B"
    with get_session_factory()() as s:
        assert s.query(CachedQuery).count() == 1


def test_expired_entry_is_refreshed(db_initialized):
    """Regressao: apos o TTL, o novo put quebrava por UNIQUE(cnpj, provider)."""
    cache_put("19131243000197", _company("A"))
    _age_rows(days=2)
    assert cache_get("19131243000197") is None
    cache_put("19131243000197", _company("B"))
    assert cache_get("19131243000197").razao_social == "B"


def test_clear(db_initialized):
    cache_put("19131243000197", _company("A"))
    assert clear_cache() == 1
    assert cache_get("19131243000197") is None
