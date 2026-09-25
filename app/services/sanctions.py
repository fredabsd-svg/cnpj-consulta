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
# Falhas tambem ficam em cache por pouco tempo: com o Portal fora do ar, cada
# pagina de empresa esperaria o timeout de novo.
_TTL_FALHA = 300
_TIMEOUT_MAX = 5.0


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
    cnpj: str,
    *,
    force_refresh: bool = False,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SanctionsCheck | None:
    """None quando a integracao esta desligada; nunca levanta excecao."""
    cfg = settings or get_settings()
    if not is_enabled(cfg):
        return None
    hit = None if force_refresh else _cache.get(cnpj)
    if hit and time.monotonic() - hit[0] < (_TTL if hit[1].consultado else _TTL_FALHA):
        return hit[1]
    result = await _fetch(cnpj, cfg, client)
    if len(_cache) > 512:
        _cache.clear()
    _cache[cnpj] = (time.monotonic(), result)
    return result


async def _fetch(cnpj: str, cfg: Settings, client: httpx.AsyncClient | None) -> SanctionsCheck:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=min(float(cfg.request_timeout_seconds), _TIMEOUT_MAX))
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
        # Sem nenhum indicador conhecido nao da para afirmar "nao consta".
        if not isinstance(data, dict) or not any(isinstance(data.get(k), bool) for k in CADASTROS):
            raise ValueError("resposta inesperada")
    except Exception as e:  # noqa: BLE001 -- vira aviso na diligencia
        log.warning("consulta de sancoes falhou: %s", type(e).__name__)
        return SanctionsCheck(False, erro="Portal da Transparência indisponível no momento")
    finally:
        if owned:
            await client.aclose()

    return SanctionsCheck(True, [nome for chave, nome in CADASTROS.items() if data.get(chave) is True])


def clear_sanctions_cache() -> None:
    _cache.clear()
