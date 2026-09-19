"""Testes da busca por socio."""

from __future__ import annotations

import pytest

from app.services.partner_search import search_partners


class TestSearchDisabled:
    def test_raises_when_disabled(self, monkeypatch):
        from app.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="RECEITA_LOCAL_ENABLED"):
            search_partners("JOAO")


class TestSearchEnabledButNoDb:
    def test_raises_when_db_missing(self, tmp_path, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        monkeypatch.setenv(
            "RECEITA_LOCAL_PATH", str(tmp_path / "nao_existe.db")
        )
        get_settings.cache_clear()

        with pytest.raises(RuntimeError, match="nao encontrada"):
            search_partners("JOAO")


class TestSearchWithDuckDB:
    def test_search_finds_partner(self, tmp_path, monkeypatch):
        import duckdb

        from app.config import get_settings

        db_path = tmp_path / "receita.db"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE socios (
                cnpj_basico VARCHAR,
                identificador_de_socio VARCHAR,
                nome_socio VARCHAR,
                nome_socio_norm VARCHAR,
                cpf_cnpj_socio VARCHAR,
                qualificacao_socio VARCHAR,
                data_entrada_sociedade VARCHAR,
                pais VARCHAR,
                representante_legal VARCHAR,
                nome_representante VARCHAR,
                qualificacao_representante_legal VARCHAR,
                faixa_etaria VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE estabelecimentos (
                cnpj_basico VARCHAR,
                cnpj_ordem VARCHAR,
                cnpj_dv VARCHAR,
                cnpj_completo VARCHAR,
                identificador_matriz_filial VARCHAR,
                nome_fantasia VARCHAR,
                situacao_cadastral VARCHAR,
                data_situacao_cadastral VARCHAR,
                motivo_situacao_cadastral VARCHAR,
                nome_cidade_exterior VARCHAR,
                pais VARCHAR,
                data_inicio_atividade VARCHAR,
                cnae_fiscal_principal VARCHAR,
                cnae_fiscal_secundaria VARCHAR,
                tipo_logradouro VARCHAR,
                logradouro VARCHAR,
                numero VARCHAR,
                complemento VARCHAR,
                bairro VARCHAR,
                cep VARCHAR,
                uf VARCHAR,
                municipio VARCHAR,
                ddd_telefone_1 VARCHAR,
                ddd_telefone_2 VARCHAR,
                numero_telefone_1 VARCHAR,
                numero_telefone_2 VARCHAR,
                correio_eletronico VARCHAR,
                situacao_especial VARCHAR,
                data_situacao_especial VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE empresas (
                cnpj_basico VARCHAR,
                razao_social VARCHAR,
                natureza_juridica VARCHAR,
                qualificacao_responsavel VARCHAR,
                capital_social VARCHAR,
                porte_empresa VARCHAR,
                ente_federativo_responsavel VARCHAR
            )
            """
        )
        conn.execute(
            "INSERT INTO socios VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["12345678", "2", "JOAO DA SILVA", "JOAO DA SILVA", "***123456**", "49", "20200101",
             None, None, None, None, "05"],
        )
        conn.execute(
            "INSERT INTO empresas VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["12345678", "EMPRESA TESTE LTDA", "2062", "05", "1000,00", "01", ""],
        )
        conn.execute(
            """INSERT INTO estabelecimentos
               (cnpj_basico, cnpj_ordem, cnpj_dv, cnpj_completo, identificador_matriz_filial,
                situacao_cadastral, uf, municipio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ["12345678", "0001", "00", "12345678000100", "1", "02", "SP", "SAO PAULO"],
        )
        # Filial da mesma empresa: NAO pode duplicar o resultado
        conn.execute(
            """INSERT INTO estabelecimentos
               (cnpj_basico, cnpj_ordem, cnpj_dv, cnpj_completo, identificador_matriz_filial,
                situacao_cadastral, uf, municipio)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ["12345678", "0002", "81", "12345678000281", "2", "02", "SP", "SAO PAULO"],
        )
        conn.close()

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        monkeypatch.setenv("RECEITA_LOCAL_PATH", str(db_path))
        get_settings.cache_clear()

        results = search_partners("joão", uf="SP")
        assert len(results) == 1
        assert results[0]["nome_socio"] == "JOAO DA SILVA"
        assert results[0]["cnpj"] == "12345678000100"
        assert results[0]["situacao"] == "ATIVA"  # codigo "02" traduzido

        assert search_partners("JOAO", uf="RJ") == []
        assert len(search_partners("JOAO", municipio="são paulo")) == 1

    def test_fallback_without_nome_socio_norm(self, tmp_path, monkeypatch):
        """Bases antigas (sem a coluna) ainda pesquisam via strip_accents."""
        import duckdb

        from app.config import get_settings

        db_path = tmp_path / "receita_old.db"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE socios (
                cnpj_basico VARCHAR, identificador_de_socio VARCHAR, nome_socio VARCHAR,
                cpf_cnpj_socio VARCHAR, qualificacao_socio VARCHAR, data_entrada_sociedade VARCHAR,
                pais VARCHAR, representante_legal VARCHAR, nome_representante VARCHAR,
                qualificacao_representante_legal VARCHAR, faixa_etaria VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE estabelecimentos (
                cnpj_basico VARCHAR, cnpj_ordem VARCHAR, cnpj_dv VARCHAR, cnpj_completo VARCHAR,
                identificador_matriz_filial VARCHAR, situacao_cadastral VARCHAR, uf VARCHAR,
                municipio VARCHAR
            )
            """
        )
        conn.execute(
            "CREATE TABLE empresas (cnpj_basico VARCHAR, razao_social VARCHAR)"
        )
        conn.execute(
            "INSERT INTO socios VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["99999999", "2", "MARIA APARECIDA", "***999999**", "49", "20200101",
             None, None, None, None, "05"],
        )
        conn.execute("INSERT INTO empresas VALUES (?, ?)", ["99999999", "ACME LTDA"])
        conn.execute(
            "INSERT INTO estabelecimentos VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["99999999", "0001", "91", "99999999000191", "1", "02", "RJ", "RIO"],
        )
        conn.close()

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        monkeypatch.setenv("RECEITA_LOCAL_PATH", str(db_path))
        get_settings.cache_clear()

        rows = search_partners("maria")
        assert len(rows) == 1
        assert rows[0]["razao_social"] == "ACME LTDA"

    def test_short_query_returns_empty(self, tmp_path, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        # Path inexistente nao e consultado: query curta retorna [] antes
        monkeypatch.setenv("RECEITA_LOCAL_PATH", str(tmp_path / "x.db"))
        get_settings.cache_clear()
        # Cria DB minimo so para passar o check de existencia
        import duckdb

        db = tmp_path / "x.db"
        duckdb.connect(str(db)).close()
        assert search_partners("ab") == []
