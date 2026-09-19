"""Adaptador para CNPJ.ws (https://publica.cnpj.ws).

API publica gratuita (3 req/min). Estrutura aninhada com `estabelecimento`
e `socios` (ja vem com cpf_cnpj_socio MASCARADO).

Endpoint: GET https://publica.cnpj.ws/cnpj/{cnpj}  (sem mascara)
"""

from __future__ import annotations

from app.providers.base import Provider, ProviderResult


class CNPJWSProvider(Provider):
    name = "cnpjws"
    is_mirror_of_rfb = True  # consolida RFB + SEFAZ + Sintegra

    @property
    def enabled(self) -> bool:
        return bool(self.settings.cnpjws_enabled)

    @property
    def base_url(self) -> str:
        return self.settings.cnpjws_base_url.rstrip("/")

    @property
    def rate_limit_per_minute(self) -> int:
        return self.settings.rate_limit_cnpjws

    def build_url(self, cnpj: str) -> str:
        # O CNPJ ja chega normalizado (sem mascara; pode conter letras).
        return f"{self.base_url}/cnpj/{cnpj}"

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
