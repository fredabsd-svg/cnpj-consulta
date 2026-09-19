"""Configuracao central via variaveis de ambiente.

Usa pydantic-settings para carregar .env e validar tipos.
Todos os parametros do app vem daqui -- nunca hardcode.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracao global."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Ambiente
    app_env: Literal["development", "production"] = "development"
    app_debug: bool = True
    app_port: int = 8000
    # Mantenha 127.0.0.1: o app nao tem login. Em production/nao-debug,
    # host nao-loopback e recusado na inicializacao (ver assert_bind_allowed).
    app_host: str = "127.0.0.1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Contato do responsavel pela instalacao (exibido na politica de privacidade)
    app_contact_email: str = ""

    # Banco principal
    database_url: str = "sqlite:///./data/app.db"

    # Base local Receita Federal
    receita_local_path: str = "./data/receita/receita.db"
    # Diretorio publico dos dados abertos; os arquivos ficam em {base}/{YYYY-MM}/
    receita_base_url: str = "https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj"

    # HTTP
    request_timeout_seconds: int = 10
    request_max_retries: int = 2
    request_backoff_seconds: float = 1.0
    # Tempo maximo que uma consulta espera por TODOS os provedores juntos.
    # Quem nao responder ate la entra como "tempo esgotado" na procedencia.
    request_total_timeout_seconds: float = 20.0

    # Limite de consultas de empresa por minuto POR IP (API + web).
    # Evita que uma unica maquina transforme o app em proxy aberto das fontes.
    inbound_rate_limit_per_minute: int = 30

    # Cache
    cache_ttl_seconds: int = 86400
    cache_backend: Literal["memory", "sqlite"] = "sqlite"

    # Rate limit por provedor (consultas/minuto)
    rate_limit_brasilapi: int = 60
    rate_limit_minha_receita: int = 30
    rate_limit_cnpjws: int = 3
    rate_limit_receitaws: int = 3

    # Provedores -- URLs e habilitacao
    brasilapi_enabled: bool = True
    brasilapi_base_url: str = "https://brasilapi.com.br/api/cnpj/v1"

    minha_receita_enabled: bool = True
    minha_receita_base_url: str = "https://minhareceita.org"

    cnpjws_enabled: bool = False
    cnpjws_base_url: str = "https://publica.cnpj.ws"
    cnpjws_api_key: str = ""

    # Opt-in: a API publica do ReceitaWS proibe republicacao/revenda (Clausula
    # Oitava). Desligado por padrao; habilite so para uso pessoal local.
    receitaws_enabled: bool = False
    receitaws_base_url: str = "https://www.receitaws.com.br/v1"

    receita_local_enabled: bool = False


def assert_bind_allowed(settings: Settings | None = None) -> None:
    """Recusa subir com host nao-loopback em production ou sem debug.

    O app e single-user local e nao tem autenticacao. Expor em 0.0.0.0
    transformaria as consultas em proxy aberto das APIs de terceiros.
    """
    cfg = settings or get_settings()
    host = (cfg.app_host or "").strip().lower()
    loopback = {"127.0.0.1", "localhost", "::1"}
    if host in loopback:
        return
    if cfg.app_env == "production" or not cfg.app_debug:
        raise RuntimeError(
            f"APP_HOST={cfg.app_host!r} nao e loopback, mas app_env={cfg.app_env!r} "
            f"e app_debug={cfg.app_debug}. O app nao tem autenticacao: use "
            "127.0.0.1/localhost, ou (apenas em laboratorio) APP_ENV=development "
            "e APP_DEBUG=true."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna singleton de Settings."""
    return Settings()
