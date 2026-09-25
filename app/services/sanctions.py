"""Sancoes federais (CEIS, CNEP, CEPIM, CEAF) pelo Portal da Transparencia.

Opcional: requer chave gratuita da API de dados (PORTAL_TRANSPARENCIA_API_KEY,
obtida em portaldatransparencia.gov.br/api-de-dados/cadastrar-email). Sem a
chave, a diligencia so lembra de conferir -- nada e consultado.

Endpoint: GET {base}/pessoa-juridica?cnpj=<14 caracteres>, cabecalho
`chave-api-dados`. A resposta traz indicadores booleanos (sancionadoCEIS,
sancionadoCNEP, ...). So o CNPJ da empresa e enviado.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

# indicador na API -> nome do cadastro exibido ao usuario
CADASTROS = {
    "sancionadoCEIS": "CEIS (inidôneas e suspensas)",
    "sancionadoCNEP": "CNEP (empresas punidas)",
    "sancionadoCEPIM": "CEPIM (entidades impedidas)",
    "sancionadoCEAF": "CEAF (expulsões da administração federal)",
}
_TTL = 6 * 3600


@dataclass(slots=True)
class SanctionsCheck:
    consultado: bool
    cadastros: list[str] = field(default_factory=list)  # onde a empresa consta
    erro: str | None = None

    @property
    def sancionada(self) -> bool:
        return bool(self.cadastros)


_cache: dict[str, tuple[float, SanctionsCheck]] = {}


def is_enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).portal_transparencia_api_key)


async def check_sanctions(
    cnpj: str, *, settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> SanctionsCheck | None:
    """None quando a integracao esta desligada; nunca levanta excecao."""
    cfg = settings or get_settings()
    if not is_enabled(cfg):
        return None
    hit = _cache.get(cnpj)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]

    owned = client is None
    client = client or httpx.AsyncClient(timeout=float(cfg.request_timeout_seconds))
    try:
        resp = await client.get(
            cfg.portal_transparencia_base_url.rstrip("/") + "/pessoa-juridica",
            params={"cnpj": cnpj},
            headers={"Accept": "application/json", "chave-api-dados": cfg.portal_transparencia_api_key},
        )
        if resp.status_code in (401, 403):
            return SanctionsCheck(False, erro="chave da API do Portal da Transparência recusada")
        if resp.status_code == 429:
            return SanctionsCheck(False, erro="limite de consultas do Portal da Transparência atingido")
        resp.raise_for_status()
        data = resp.json() or {}
        if not isinstance(data, dict):
            raise ValueError("resposta inesperada")
    except Exception as e:  # noqa: BLE001 -- vira aviso na diligencia
        log.warning("consulta de sancoes falhou: %s", type(e).__name__)
        return SanctionsCheck(False, erro="Portal da Transparência indisponível no momento")
    finally:
        if owned:
            await client.aclose()

    result = SanctionsCheck(True, [nome for chave, nome in CADASTROS.items() if data.get(chave) is True])
    if len(_cache) > 512:
        _cache.clear()
    _cache[cnpj] = (time.monotonic(), result)
    return result


def clear_sanctions_cache() -> None:
    _cache.clear()
