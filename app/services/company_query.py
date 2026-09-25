"""Orquestrador de consulta: cache + provedores em paralelo + conciliacao."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.core.cnpj_validator import normalize
from app.providers.registry import ProviderRegistry, get_registry
from app.schemas.company import CompanyUnified
from app.services.cache import cache_get, cache_put
from app.services.history_service import record_query
from app.services.reconciliation import reconcile

log = logging.getLogger(__name__)


async def query_company_async(
    cnpj: str,
    registry: ProviderRegistry | None = None,
    *,
    force_refresh: bool = False,
    on_miss: Callable[[], None] | None = None,
) -> CompanyUnified:
    """Consulta assincrona. Pode ser chamada de dentro de event loop (FastAPI).

    Levanta ValueError se o CNPJ for invalido -- nunca consulta provedores
    externos com entrada mal formada. `on_miss` e chamado antes de ir as
    fontes (so quando o cache nao serve); se levantar excecao, a consulta
    e abortada sem tocar os provedores (usado pelo limite da consulta em lote).
    """
    normalized = normalize(cnpj)
    registry = registry or get_registry()

    # SQLite e sincrono: roda em thread para nao travar o servidor.
    unified = None if force_refresh else await asyncio.to_thread(cache_get, normalized)
    if unified is not None:
        log.info("cache hit para %s", normalized)
        unified.origem_cache = True
    else:
        if on_miss is not None:
            on_miss()
        results = await registry.fetch_all_parallel(normalized)
        ok = [r for r in results if r.http_status == 200]
        if not ok:
            # Sem corpo de resposta: so status/provedor -- evita vazar payload.
            summary = ", ".join(f"{r.provider}:{r.http_status}" for r in results) or (
                "(nenhum provedor habilitado)"
            )
            log.warning(
                "nenhuma fonte OK para %s; procedencia parcial/vazia [%s]",
                normalized,
                summary,
            )
        unified = reconcile(normalized, results)
        if ok:
            try:
                await asyncio.to_thread(cache_put, normalized, unified)
            except Exception as e:  # noqa: BLE001
                log.warning("falha ao salvar cache: %s", e)

    # O historico registra toda consulta do usuario, inclusive as do cache.
    try:
        await asyncio.to_thread(
            record_query,
            cnpj=normalized,
            razao_social=unified.razao_social,
            situacao=unified.situacao_cadastral,
            sources=",".join(f.fonte for f in unified.fontes if f.status == 200),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("falha ao registrar historico: %s", e)

    return unified


def query_company(
    cnpj: str, registry: ProviderRegistry | None = None, *, force_refresh: bool = False
) -> CompanyUnified:
    """Wrapper sincrono para CLI e testes (nao pode ser chamado de dentro de loop)."""

    async def _run() -> CompanyUnified:
        reg = registry or get_registry()
        try:
            return await query_company_async(cnpj, reg, force_refresh=force_refresh)
        finally:
            # Cada asyncio.run cria um loop novo; clientes HTTP presos ao loop
            # anterior nao podem ser reaproveitados.
            await reg.aclose()

    return asyncio.run(_run())
