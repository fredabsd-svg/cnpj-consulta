"""Adaptador para Minha Receita (https://minhareceita.org).

Minha Receita e a FONTE PRIMARIA recomendada:
- Sem chave de API
- Sem restricoes comerciais
- Dados completos (QSA com documento mascarado conforme a RFB)
- Atualizacao mensal a partir dos CSVs da Receita Federal
- Licenca MIT do codigo

Formatos:
- URL: GET https://minhareceita.org/{cnpj}  (com ou sem mascara)
- Resposta: snake_case, datas ISO
"""

from __future__ import annotations

from app.providers.base import Provider, ProviderResult


class MinhaReceitaProvider(Provider):
    name = "minha_receita"
    is_mirror_of_rfb = False  # FONTE PRIMARIA

    @property
    def enabled(self) -> bool:
        return bool(self.settings.minha_receita_enabled)

    @property
    def base_url(self) -> str:
        return self.settings.minha_receita_base_url.rstrip("/")

    @property
    def rate_limit_per_minute(self) -> int:
        return self.settings.rate_limit_minha_receita

    def build_url(self, cnpj: str) -> str:
        return f"{self.base_url}/{cnpj}"

    async def fetch_company(self, cnpj: str) -> ProviderResult:
        async def _do() -> ProviderResult:
            url = self.build_url(cnpj)
            status, body = await self._request_json(url)
            self._raise_for_status(status, body)
            # A API nao expoe data de atualizacao por empresa -- a defasagem
            # e a da base mensal. Nao inventamos precisao: updated_at=None.
            return ProviderResult(
                provider=self.name,
                raw=body,
                http_status=status,
                url=url,
                is_mirror_of_rfb=self.is_mirror_of_rfb,
            )

        return await self._timed(_do())
