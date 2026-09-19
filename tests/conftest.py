"""Fixtures compartilhadas pelos testes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Cada teste roda em ambiente isolado: .env apontando para tmp."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "receita").mkdir(exist_ok=True)

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    monkeypatch.setenv("RECEITA_LOCAL_PATH", str(tmp_path / "data" / "receita" / "receita.db"))
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    monkeypatch.setenv("BRASILAPI_ENABLED", "true")
    monkeypatch.setenv("MINHA_RECEITA_ENABLED", "true")
    monkeypatch.setenv("CNPJWS_ENABLED", "false")
    monkeypatch.setenv("RECEITAWS_ENABLED", "true")
    monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "false")
    monkeypatch.setenv("CACHE_BACKEND", "sqlite")
    # Sem esperas reais entre retentativas nos testes
    monkeypatch.setenv("REQUEST_BACKOFF_SECONDS", "0")

    # Limpa caches singletons
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.registry import get_registry

    get_registry.cache_clear()

    # Reseta engine/Factory do SQLAlchemy para apontar para o DB isolado
    import app.db as _db

    _db._engine = None
    _db._SessionLocal = None

    yield

    # Limpa caches no teardown tambem (para o proximo teste)
    get_settings.cache_clear()
    get_registry.cache_clear()
    _db._engine = None
    _db._SessionLocal = None


@pytest.fixture
def db_initialized(tmp_path: Path) -> Iterator[Path]:
    """Inicializa o banco SQLite principal para o teste."""
    from app.db import init_database

    init_database()
    yield tmp_path / "data" / "app.db"


@pytest.fixture
def sample_cnpj() -> str:
    """CNPJ usado nos testes -- da Open Knowledge Brasil (valido)."""
    return "19131243000197"


@pytest.fixture
def sample_formatted_cnpj() -> str:
    return "19.131.243/0001-97"


@pytest.fixture
def brasilapi_payload() -> dict:
    """Payload da BrasilAPI para 19.131.243/0001-97 (Open Knowledge Brasil).

    Dados da pessoa juridica sao publicos; nome e CPF mascarado do socio sao ficticios.
    """
    return {
        "uf": "SP",
        "cep": "01311902",
        "qsa": [
            {
                "pais": None,
                "nome_socio": "MARIA EXEMPLO DA SILVA",
                "codigo_pais": None,
                "faixa_etaria": "Entre 41 a 50 anos",
                "cnpj_cpf_do_socio": "***123456**",
                "qualificacao_socio": "Presidente",
                "codigo_faixa_etaria": 5,
                "data_entrada_sociedade": "2024-02-27",
                "identificador_de_socio": 2,
                "cpf_representante_legal": "***000000**",
                "nome_representante_legal": "",
                "codigo_qualificacao_socio": 16,
                "qualificacao_representante_legal": "Nao informada",
                "codigo_qualificacao_representante_legal": 0,
            }
        ],
        "cnpj": "19131243000197",
        "razao_social": "OPEN KNOWLEDGE BRASIL",
        "nome_fantasia": "REDE PELO CONHECIMENTO LIVRE",
        "porte": "DEMAIS",
        "codigo_porte": 5,
        "municipio": "SAO PAULO",
        "logradouro": "PAULISTA 37",
        "numero": "37",
        "complemento": "ANDAR 4",
        "bairro": "BELA VISTA",
        "cnae_fiscal": 9430800,
        "cnae_fiscal_descricao": "Atividades de associacoes de defesa de direitos sociais",
        "cnaes_secundarios": [{"codigo": 9493600, "descricao": "Atividades de organizacoes associativas ligadas a cultura e a arte"}],
        "natureza_juridica": "Associacao Privada",
        "codigo_natureza_juridica": 3999,
        "capital_social": 0,
        "ddd_telefone_1": "1123851939",
        "situacao_cadastral": 2,
        "descricao_situacao_cadastral": "ATIVA",
        "data_situacao_cadastral": "2013-10-03",
        "data_inicio_atividade": "2013-10-03",
        "identificador_matriz_filial": 1,
        "descricao_identificador_matriz_filial": "MATRIZ",
    }


@pytest.fixture
def receitaws_payload() -> dict:
    return {
        "abertura": "03/10/2013",
        "situacao": "ATIVA",
        "tipo": "MATRIZ",
        "nome": "OPEN KNOWLEDGE BRASIL",
        "fantasia": "REDE PELO CONHECIMENTO LIVRE",
        "porte": "DEMAIS",
        "natureza_juridica": "399-9 - Associacao Privada",
        "atividade_principal": [{"code": "94.30-8-00", "text": "Atividades de associacoes"}],
        "atividades_secundarias": [],
        "qsa": [{"nome": "MARIA EXEMPLO DA SILVA", "qual": "16-Presidente"}],
        "logradouro": "AVENIDA PAULISTA 37",
        "numero": "37",
        "municipio": "SAO PAULO",
        "bairro": "BELA VISTA",
        "uf": "SP",
        "cep": "01.311-902",
        "email": "contato@example.org",
        "telefone": "(11) 2385-1939",
        "data_situacao": "03/10/2013",
        "cnpj": "19.131.243/0001-97",
        "ultima_atualizacao": "2026-09-15T23:59:59.000Z",
        "status": "OK",
        "motivo_situacao": "",
        "capital_social": "0.00",
        "simples": {"optante": False, "data_opcao": None, "data_exclusao": None},
        "simei": {"optante": False, "data_opcao": None, "data_exclusao": None},
        "billing": {"free": True, "database": True},
    }
