"""Adaptador para ReceitaWS (https://www.receitaws.com.br).

ATENCAO: a API PUBLICA proibe revenda / republicacao dos dados via
integracao para terceiros (Clausula Oitava do termo). Para uso pessoal
ou local esta OK; para redistribuir publicamente, contratacao comercial.

Usada como FALLBACK secundario (nao primaria) por causa de:
- ausencia de CPF no QSA (apenas nome + qualificacao)
- restricao comercial
- limite de 3 consultas/minuto no plano gratuito

Formato:
- URL: GET https://www.receitaws.com.br/v1/cnpj/{cnpj}
- Datas dd/mm/yyyy, capital social "1000.00", CEP/telefone com mascara
"""

from __future__ import annotations

from datetime import datetime

from app.providers.base import Provider, ProviderError, ProviderResult


class ReceitaWSProvider(Provider):
    name = "receitaws"
    is_mirror_of_rfb = True  # derivada da RFB (nao e a RFB direta)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.receitaws_enabled)

    @property
    def base_url(self) -> str:
        return self.settings.receitaws_base_url.rstrip("/")

    @property
    def rate_limit_per_minute(self) -> int:
        return self.settings.rate_limit_receitaws

    @property
    def timeout(self) -> float:
        # ReceitaWS costuma ser mais lento -- timeout maior
        return float(self.settings.request_timeout_seconds) * 1.5

    def build_url(self, cnpj: str) -> str:
        return f"{self.base_url}/cnpj/{cnpj}"

    async def fetch_company(self, cnpj: str) -> ProviderResult:
        async def _do() -> ProviderResult:
            url = self.build_url(cnpj)
            status, body = await self._request_json(url)
            self._raise_for_status(status, body)
            if body.get("status") == "ERROR":
                raise ProviderError(
                    str(body.get("message") or "CNPJ nao encontrado nesta fonte"),
                    status=404,
                    source=self.name,
                )
            updated = None
            if isinstance(body.get("ultima_atualizacao"), str):
                try:
                    updated = datetime.fromisoformat(body["ultima_atualizacao"].replace("Z", "+00:00"))
                except ValueError:
                    updated = None
            return ProviderResult(
                provider=self.name,
                raw=body,
                http_status=status,
                url=url,
                updated_at=updated,
                is_mirror_of_rfb=self.is_mirror_of_rfb,
                extra={"from_database": bool((body.get("billing") or {}).get("database"))},
            )

        return await self._timed(_do())
