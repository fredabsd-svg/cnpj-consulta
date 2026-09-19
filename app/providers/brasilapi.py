"""Adaptador para BrasilAPI (https://brasilapi.com.br).

IMPORTANTE: BrasilAPI e um PROXY da API Minha Receita (a propria spec
declara: "Busca por CNPJ na API Minha Receita."). NAO deve ser tratada
como fonte independente no agregado -- marcamos is_mirror_of_rfb=True
para que a tabela de procedencia indique isso ao usuario, e o conciliador
agrupa BrasilAPI com Minha Receita ao calcular a confianca.

Quando estiver habilitada, ainda assim fazemos a consulta para fornecer
fallback de disponibilidade.
"""

from __future__ import annotations

from app.providers.base import Provider, ProviderResult


class BrasilAPIProvider(Provider):
    name = "brasilapi"
    is_mirror_of_rfb = True  # proxy da Minha Receita -- espelho

    @property
    def enabled(self) -> bool:
        return bool(self.settings.brasilapi_enabled)

    @property
    def base_url(self) -> str:
        return self.settings.brasilapi_base_url.rstrip("/")

    @property
    def rate_limit_per_minute(self) -> int:
        return self.settings.rate_limit_brasilapi

    def build_url(self, cnpj: str) -> str:
        return f"{self.base_url}/{cnpj}"

    async def fetch_company(self, cnpj: str) -> ProviderResult:
        async def _do() -> ProviderResult:
            url = self.build_url(cnpj)
            status, body = await self._request_json(url)
            self._raise_for_status(status, body)
            return ProviderResult(
                provider=self.name,
                raw=body,
                http_status=status,
                url=url,
                is_mirror_of_rfb=self.is_mirror_of_rfb,
            )

        return await self._timed(_do())
