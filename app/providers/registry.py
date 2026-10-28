"""Registro de provedores habilitados.

Cria instancias dos provedores conforme Settings e expoe uma interface
para o orquestrador consultar.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import lru_cache

import httpx

from app.config import Settings, get_settings
from app.providers.base import Provider, ProviderError, ProviderResult
from app.providers.brasilapi import BrasilAPIProvider
from app.providers.cnpjws import CNPJWSProvider
from app.providers.minha_receita import MinhaReceitaProvider
from app.providers.receita_local import ReceitaLocalProvider
from app.providers.receitaws import ReceitaWSProvider

log = logging.getLogger(__name__)

PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    ReceitaLocalProvider,
    MinhaReceitaProvider,
    BrasilAPIProvider,
    CNPJWSProvider,
    ReceitaWSProvider,
)


class ProviderRegistry:
    """Colecao de provedores habilitados."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._providers: list[Provider] = []
        self._disabled: list[Provider] = []
        for cls in PROVIDER_CLASSES:
            try:
                p = cls(settings)
            except Exception:  # pragma: no cover -- defensivo
                log.exception("Falha ao instanciar %s", cls.__name__)
                continue
            # A base local pode ser instalada com o servidor rodando: fica na
            # lista e a disponibilidade e conferida a cada consulta.
            dinamico = isinstance(p, ReceitaLocalProvider) and settings.receita_local_enabled
            (self._providers if p.enabled or dinamico else self._disabled).append(p)

    async def aclose(self) -> None:
        for p in self._providers:
            await p.aclose()

    def all(self) -> list[Provider]:
        return [p for p in self._providers if p.enabled]

    def disabled(self) -> list[Provider]:
        """Fontes conhecidas mas desligadas no .env (para a pagina de status)."""
        return list(self._disabled) + [p for p in self._providers if not p.enabled]

    def by_name(self, name: str) -> Provider | None:
        return next((p for p in self.all() if p.name == name), None)

    async def fetch_all_parallel(self, cnpj: str, *, nomes: set[str] | None = None) -> list[ProviderResult]:
        """Consulta todos os provedores habilitados em paralelo.

        Cada provedor que falhar (ou estourar o tempo total) NAO interrompe os
        demais -- a falha vira um ProviderResult com erro na procedencia.
        """
        budget = self.settings.request_total_timeout_seconds
        alvos = [p for p in self.all() if nomes is None or p.name in nomes]
        tasks = [self._safe_fetch(p, cnpj, budget) for p in alvos]
        return list(await asyncio.gather(*tasks))

    @staticmethod
    async def _safe_fetch(provider: Provider, cnpj: str, budget: float) -> ProviderResult:
        try:
            return await asyncio.wait_for(provider.fetch_company(cnpj), timeout=budget)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, TimeoutError):
                message = f"sem resposta em {budget:.0f}s (tempo esgotado)"
            elif isinstance(e, ProviderError):
                message = str(e)
            elif isinstance(e, httpx.HTTPError):
                message = f"falha de conexao ({type(e).__name__})"
            else:
                message = f"erro inesperado ({type(e).__name__}: {e})"
            status = e.status if isinstance(e, ProviderError) and e.status else 0
            return ProviderResult(
                provider=provider.name,
                raw={},
                http_status=status,
                url=provider.build_url(cnpj),
                fetched_at=datetime.now(UTC),
                is_mirror_of_rfb=provider.is_mirror_of_rfb,
                extra={"error": message[:200]},
            )


@lru_cache(maxsize=1)
def get_registry() -> ProviderRegistry:
    return ProviderRegistry(get_settings())
