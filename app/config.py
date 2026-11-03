"""Configuracao central via variaveis de ambiente.

Usa pydantic-settings para carregar .env e validar tipos.
Todos os parametros do app vem daqui -- nunca hardcode.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
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
    # Prefira 127.0.0.1 e publique com `tailscale serve` (ver MCP.md). Host
    # nao-loopback so e aceito com APP_LOGIN_REMOTO=true (ver assert_bind_allowed).
    app_host: str = "127.0.0.1"
    # Acesso que nao vem do proprio computador (Tailscale, tunel, rede local) exige
    # o login de administrador em todas as paginas. No proprio PC (localhost), nao.
    app_login_remoto: bool = True
    # Nomes de host (alem de localhost e enderecos IP) aceitos no cabecalho Host,
    # separados por virgula: ex. "meuservidor.local". Protege contra DNS rebinding.
    app_hosts_permitidos: str = ""
    # So true atras de um proxy reverso seu: passa a usar o X-Forwarded-For como IP
    # do cliente (limite de login e auditoria). Sem proxy, o cabecalho e ignorado.
    confiar_proxy: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Contato do responsavel pela instalacao (exibido na politica de privacidade)
    app_contact_email: str = ""

    # Banco principal
    database_url: str = "sqlite:///./data/app.db"

    # Base local Receita Federal (dados abertos do CNPJ)
    # Pasta com as versoes publicadas, o ponteiro ATIVA.json e a area de trabalho
    # das atualizacoes. Precisa de ~45 GB livres durante uma atualizacao.
    receita_data_dir: str = "./data/receita"
    # Base avulsa (formato antigo ou copiada de outra maquina). So e usada quando
    # ainda nao ha versao publicada em receita_data_dir.
    receita_local_path: str = "./data/receita/receita.db"
    # Ponto de entrada OFICIAL: o portal de arquivos da Receita. O endereco do
    # compartilhamento e descoberto a partir dele (redirecionamento) e a pasta
    # do CNPJ e confirmada pela listagem WebDAV, sem URLs fixas de arquivos.
    receita_portal_url: str = "https://arquivos.receitafederal.gov.br/"
    # Caminho da pasta do CNPJ dentro do compartilhamento. Se nao existir mais,
    # o app procura uma pasta "CNPJ" com subpastas AAAA-MM.
    receita_pasta_cnpj: str = "Dados/Cadastros/CNPJ"
    # Forca um token de compartilhamento (so se a descoberta automatica falhar).
    receita_share_token: str = ""
    # Limites do DuckDB durante a importacao (nunca carrega a base inteira na RAM:
    # o excedente vai para disco temporario na area de trabalho).
    receita_duckdb_memoria: str = "4GB"
    receita_duckdb_threads: int = 0  # 0 = automatico
    receita_manter_zips: bool = False
    # Conexoes simultaneas de download. Arquivos grandes sao baixados em partes
    # em paralelo (o portal limita a velocidade por conexao). 1 = sequencial.
    receita_downloads_paralelos: int = 4
    # Pasta para os CSVs extraidos e o temporario do DuckDB. Vazio = area de
    # trabalho dentro de RECEITA_DATA_DIR. Aponte para um disco local rapido
    # (SSD) quando os dados ficam num disco externo ou de rede.
    receita_temp_dir: str = ""
    receita_versoes_mantidas: int = 2  # ativa + anterior (para restaurar)
    # Validacao: recusa publicar se uma tabela principal encolher mais que isto
    # em relacao a versao ativa (protege contra arquivo truncado na origem).
    receita_queda_maxima_pct: float = 10.0
    receita_orfaos_max_pct: float = 5.0
    # Consulta local primeiro: se o CNPJ estiver na base local, as fontes
    # externas so sao chamadas quando o usuario pedir.
    receita_local_primeiro: bool = True

    # Area administrativa (base de dados, contatos complementares, auditoria).
    # Vazio = token gerado no primeiro uso em data/admin_token.txt.
    admin_token: str = ""
    admin_sessao_horas: int = 8
    # Chave (Fernet) do cofre de CPFs completos. Vazio = gerada em data/cpf.key.
    # Guarde uma copia: sem ela os CPFs cadastrados nao podem ser lidos.
    cpf_chave: str = ""

    # Servidor MCP: ferramentas que alteram dados so com true (ver MCP.md)
    mcp_permitir_admin: bool = False
    # Chave do MCP pela rede (/mcp). Vazio = gerada em data/mcp_chave.txt.
    mcp_chave: str = ""

    # Chat com IA (/chat, so administrador; ver CHAT_IA.md). As chaves tambem
    # podem ser cadastradas na propria pagina do chat. Vazio = provedor desligado.
    chat_habilitado: bool = True
    chat_provedor_padrao: str = "anthropic"
    chat_modelo_padrao: str = ""  # vazio = primeiro modelo do provedor
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    deepseek_api_key: str = ""
    groq_api_key: str = ""
    mistral_api_key: str = ""
    xai_api_key: str = ""
    # Modelos locais (sem chave). Vazio = desligado; ex.: http://localhost:11434/v1
    ollama_url: str = ""
    lmstudio_url: str = ""
    # Qualquer outro servidor compativel com a API da OpenAI (vLLM, LiteLLM, Azure...)
    chat_custom_url: str = ""
    chat_custom_api_key: str = ""
    chat_custom_modelos: str = ""  # separados por virgula
    # Esforco de raciocinio dos modelos Claude: low | medium | high | xhigh | max
    chat_esforco: str = "medium"
    chat_max_tokens: int = 64000
    # Chamadas de ferramenta por mensagem (protege contra lacos sem fim)
    chat_max_rodadas: int = 25
    # Resultado de ferramenta maior que isto e cortado antes de ir ao modelo
    chat_resultado_max_chars: int = 30000
    chat_sql_max_linhas: int = 500
    chat_sql_timeout_segundos: float = 30.0

    # Busca avancada
    # Tempo maximo de cada consulta da busca (tela, API, MCP); a tela tem o botao
    # Parar. Exportacoes rodam em segundo plano, sem este limite.
    busca_timeout_segundos: float = 60.0
    # Consultas pesadas ao mesmo tempo (as demais esperam a vez): evita que a tela,
    # o chat e o MCP juntos esgotem a memoria do computador.
    busca_simultaneas: int = 2
    # Teto do disco temporario das consultas (o que nao cabe na memoria)
    busca_temp_max: str = "20GB"
    busca_max_por_pagina: int = 100
    exportacao_max_linhas: int = 0  # 0 = sem limite (o Excel e dividido em abas de 1 milhao)
    # Acima disto a exportacao vira tarefa em segundo plano.
    exportacao_sincrona_max: int = 20_000

    # O .env criado pelo instalador copiava estes valores antigos de fabrica (30 s e
    # 500 mil linhas), que derrubavam buscas e cortavam exportacoes em bases grandes:
    # exatamente esses valores passam a valer o padrao novo.
    @field_validator("busca_timeout_segundos")
    @classmethod
    def _timeout_antigo(cls, v: float) -> float:
        return 60.0 if v == 30 else v

    @field_validator("exportacao_max_linhas")
    @classmethod
    def _teto_antigo(cls, v: int) -> int:
        return 0 if v == 500_000 else v

    # Contatos complementares (fontes fora da Receita, sempre com origem)
    contatos_desatualizado_dias: int = 365
    contatos_retencao_dias: int = 730

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

    # API publica gratuita (3 req/min); sem autenticacao.
    # cnpjws_api_key removida: nunca foi lida pelo adaptador CNPJ.ws.
    cnpjws_enabled: bool = False
    cnpjws_base_url: str = "https://publica.cnpj.ws"

    # Opt-in: a API publica do ReceitaWS proibe republicacao/revenda (Clausula
    # Oitava). Desligado por padrao; habilite so para uso pessoal local.
    receitaws_enabled: bool = False
    receitaws_base_url: str = "https://www.receitaws.com.br/v1"

    # Usa a base local quando ela existir (publicada pelo painel "Base de Dados
    # CNPJ" ou pelo comando `cnpj base atualizar`).
    receita_local_enabled: bool = True

    # Pesquisa na internet (opcional). Vazio = so atalhos de pesquisa (links).
    # "tavily": Tavily (1.000 buscas/mes gratis; chave em WEB_SEARCH_API_KEY);
    # "brave": Brave Search API (credito mensal; chave em WEB_SEARCH_API_KEY);
    # "searxng": instancia SearXNG propria (WEB_SEARCH_URL) com formato json.
    # A consulta so parte quando o usuario clica em "Pesquisar" e usa apenas
    # dados da empresa -- nunca nomes de socios (LGPD).
    web_search_provider: Literal["", "tavily", "brave", "searxng"] = ""
    web_search_api_key: str = ""
    web_search_url: str = ""
    web_search_max_results: int = 8
    web_search_cache_seconds: int = 3600

    # Portal da Transparencia (CEIS/CNEP: empresas sancionadas). Chave gratuita
    # em portaldatransparencia.gov.br/api-de-dados/cadastrar-email. Vazio = desligado.
    portal_transparencia_api_key: str = ""
    portal_transparencia_base_url: str = "https://api.portaldatransparencia.gov.br/api-de-dados"

    # Consulta em lote: maximo de CNPJs por pedido.
    batch_max_items: int = 30

    # Inscricao estadual automatica (opcional): web service de consulta cadastral
    # da SEFAZ com o certificado digital A1 (.pfx) do escritorio. Sem isso, o app
    # abre o portal CCC (login gov.br) para a consulta manual.
    certificado_a1_path: str = ""
    certificado_a1_senha: str = ""
    # Cadeia de CAs extra (ex.: ICP-Brasil) se a SEFAZ usar certificado de
    # servidor fora do repositorio do sistema.
    sefaz_ca_bundle: str = ""


def host_de_execucao(argv: list[str] | None = None) -> str | None:
    """Endereco passado ao uvicorn na linha de comando (`--host X` ou `--host=X`)
    ou em UVICORN_HOST: e nele que o servidor escuta de fato, nao no APP_HOST."""
    import os
    import sys

    args = sys.argv if argv is None else argv
    host = None
    for i, a in enumerate(args):  # o uvicorn/click usa o ULTIMO --host repetido
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
    return host or os.environ.get("UVICORN_HOST") or None


def assert_bind_allowed(settings: Settings | None = None, host_real: str | None = None) -> None:
    """Recusa subir com host nao-loopback sem o login para acesso remoto.

    Com APP_LOGIN_REMOTO=true (padrao) todo acesso que nao vem do proprio
    computador exige login, entao ouvir na rede (ex.: IP do Tailscale) e aceito.
    Sem ele, so em laboratorio (APP_ENV=development e APP_DEBUG=true).
    Confere o APP_HOST e tambem o `--host` real do uvicorn.
    """
    cfg = settings or get_settings()
    loopback = {"127.0.0.1", "localhost", "::1"}
    for valor in (cfg.app_host, host_real):
        host = (valor or "").strip().lower().strip("[]")
        if valor is None or host in loopback or cfg.app_login_remoto:
            continue
        if cfg.app_env != "production" and cfg.app_debug:
            continue
        raise RuntimeError(
            f"Endereco {valor!r} nao e loopback, mas app_env={cfg.app_env!r} "
            f"e app_debug={cfg.app_debug} com APP_LOGIN_REMOTO=false: use "
            "127.0.0.1/localhost, ligue APP_LOGIN_REMOTO=true, ou (apenas em "
            "laboratorio) APP_ENV=development e APP_DEBUG=true."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna singleton de Settings."""
    return Settings()
