"""SQL do chat depois das buscas (conexao compartilhada) e filtro 'E-mail contem'."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import busca_avancada as ba


def test_sql_do_chat_continua_funcionando_depois_das_buscas(base_local):
    """Antes, a primeira consulta SQL do chat travava a configuracao da conexao
    compartilhada e as seguintes (e descrever_bancos) falhavam com
    InvalidInputException."""
    from app.chat import sql

    for _ in range(3):
        assert sql.consultar_base("SELECT count(*) AS n FROM estabelecimentos")["linhas"] == [[4]]
        assert ba.contar(ba.Filtros.de_params({"uf": ["TO"]})) == 3
        assert "estabelecimentos" in sql.esquema_base()["tabelas"]


def test_conexao_de_leitura_sem_acesso_externo_e_travada(base_local):
    from app.chat import sql
    from app.receita import duck
    from app.receita.armazenamento import base_ativa

    with duck.conectar_leitura(base_ativa().caminho) as c:
        with pytest.raises(duckdb.Error):
            c.execute("SET enable_external_access = true")
        with pytest.raises(duckdb.Error):
            c.execute("SELECT * FROM read_csv('/etc/passwd')").fetchall()
    with pytest.raises(sql.ConsultaInvalida):
        sql.consultar_base("SELECT * FROM read_csv('/etc/passwd')")


def test_filtro_email_contem(base_local):
    f = ba.Filtros.de_params
    assert ba.contar(f({"email_contem": "contafacil"})) == 1
    assert ba.contar(f({"email_contem": "CONTATO@"})) == 1  # maiusculas e o @ valem
    assert ba.contar(f({"email_contem": ["contafacil", "mercado"]})) == 2  # OU
    assert ba.contar(f({"email_contem": "gmail"})) == 1
    assert ba.contar(f({"email_contem": "%%%"})) == 0  # curinga e texto, nao padrao
    with pytest.raises(ba.FiltroInvalido):
        ba.contar(f({"email_contem": "ab"}))
    r = TestClient(app).get("/api/busca", params={"email_contem": "contafacil", "colunas": ["cnpj", "email"]}).json()
    assert [i["email"] for i in r["itens"]] == ["contato@contafacil.com.br"]
    assert 'name="email_contem"' in TestClient(app).get("/busca").text


async def test_sequencia_do_chat_que_falhava(base_local):
    import json

    from app.chat import ferramentas

    passos = [
        ("descrever_filtros", {}),
        ("descrever_bancos", {}),
        ("consultar_sql_base", {"sql": "SELECT cnpj_completo, email FROM estabelecimentos WHERE email ILIKE '%contafacil%'"}),
        ("status_base_local", {}),
        ("buscar_empresas", {"filtros": {"email_contem": ["contafacil"]}}),
        ("consultar_sql_base", {"sql": "SELECT count(*) AS n FROM estabelecimentos"}),
    ]
    for nome, entrada in passos:
        ok, txt = await ferramentas.executar(nome, entrada, max_chars=50_000)
        assert ok, (nome, txt[:300])
    dados = json.loads(txt)
    assert dados["linhas"] == [[4]]
    ok, txt = await ferramentas.executar("buscar_empresas", {"filtros": {"email_contem": ["contafacil"]}}, max_chars=50_000)
    assert json.loads(txt)["total"] == 1
