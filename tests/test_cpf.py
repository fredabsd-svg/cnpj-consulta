"""Acesso a CPF completo: cofre (cadastro e fonte externa), titular de MEI, busca
por CPF, telas e ferramentas -- sempre so para o administrador e auditado."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app

CPF_MARIA = "98712345628"  # digitos 4-9 = 123456 (MARIA DA SILVA, ***123456** na base de teste)
CPF_JOAO = "11165432145"  # digitos 4-9 = 654321 (JOAO SOUZA, ***654321**)
CONTABIL = "12345678000195"
MEI = "11222333000181"  # razao social "JOSE PEREIRA 12345678900", natureza 2135


class TestRegras:
    def test_validacao_e_mascara(self):
        from app.services import cpf

        assert cpf.cpf_valido(CPF_MARIA) and cpf.cpf_valido("987.123.456-28")
        assert not cpf.cpf_valido("98712345600") and not cpf.cpf_valido("11111111111")
        assert cpf.mascarar(CPF_MARIA) == "***.123.456-**"
        assert cpf.digitos_visiveis("***123456**") == cpf.digitos_visiveis("***.123.456-**") == "123456"
        assert cpf.chave_pessoa("Maria da Silva", "***.123.456-**") == cpf.chave_pessoa("MARIA DA SILVA", "***123456**")

    def test_cpf_do_titular(self):
        from app.services import cpf

        assert cpf.cpf_do_titular("JOSE PEREIRA 12345678900", 2135) == "123.456.789-00"
        assert cpf.cpf_do_titular("JOSE PEREIRA 12345678900", 2062) is None  # nao e empresario individual
        assert cpf.cpf_do_titular("JOSE PEREIRA 98712345628", None) == "987.123.456-28"
        assert cpf.cpf_do_titular("PADARIA 12345678900", None) is None  # sem natureza: exige DV


class TestCofre:
    def test_cadastro_confere_com_digitos_publicados(self, db_initialized):
        from app.services import cpf

        with pytest.raises(cpf.CpfInvalido, match="não confere"):
            cpf.cadastrar("MARIA DA SILVA", CPF_JOAO, fonte="contrato social", finalidade="eSocial do cliente",
                          documento_publicado="***123456**")
        with pytest.raises(cpf.CpfInvalido, match="inválido"):
            cpf.cadastrar("MARIA DA SILVA", "98712345600", fonte="contrato", finalidade="eSocial do cliente")
        with pytest.raises(cpf.CpfInvalido, match="finalidade"):
            cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato", finalidade="x")
        r = cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente",
                          documento_publicado="***123456**")
        assert r["conferido"] and r["cpf_mascarado"] == "***.123.456-**" and "cpf" not in r
        achado = cpf.revelar([("Maria da Silva", "***.123.456-**")])
        assert list(achado.values())[0]["cpf"] == "987.123.456-28"
        assert cpf.por_cpf("987.123.456-28")["nome"] == "MARIA DA SILVA"

    def test_cpf_guardado_criptografado(self, db_initialized, tmp_path):
        from app.services import cpf

        cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente")
        bruto = json.dumps(sqlite3.connect(tmp_path / "data" / "app.db").execute(
            "SELECT * FROM socio_cpf").fetchall())
        assert CPF_MARIA not in bruto and "987.123.456-28" not in bruto

    def test_chat_sql_nao_le_o_cofre(self, db_initialized):
        from app.chat import sql
        from app.services import cpf

        cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente")
        with pytest.raises(sql.ConsultaInvalida):
            sql.consultar_app("SELECT * FROM socio_cpf")


class TestTelas:
    def test_ficha_mostra_cpf_completo_so_ao_admin_e_audita(self, base_local, admin_headers):
        from app.receita import jobs
        from app.services import cpf

        cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente",
                      documento_publicado="***123456**")
        c = TestClient(app)
        publico = c.get(f"/empresa/{CONTABIL}").text
        assert "987.123.456-28" not in publico and "***.123.456-**" in publico
        assert not [a for a in jobs.auditoria() if a["acao"] == "cpf.visualizar"]
        admin = c.get(f"/empresa/{CONTABIL}", headers=admin_headers).text
        assert "987.123.456-28" in admin
        reg = [a for a in jobs.auditoria() if a["acao"] == "cpf.visualizar"]
        assert reg and "quantidade=1" in reg[0]["detalhe"] and CPF_MARIA not in reg[0]["detalhe"]

    def test_cpf_do_titular_na_ficha(self, base_local):
        assert "123.456.789-00" in TestClient(app).get(f"/empresa/{MEI}").text

    def test_cadastro_pela_pagina_do_socio(self, base_local, admin_headers):
        from app.services import ficha

        chave = next(s["chave"] for s in ficha.ficha_local(CONTABIL)["socios"] if s["nome"] == "JOAO SOUZA")
        c = TestClient(app)
        assert "Cadastrar o CPF completo" not in c.get(f"/socio/{chave}").text  # nao-admin
        assert "Cadastrar o CPF completo" in c.get(f"/socio/{chave}", headers=admin_headers).text
        r = c.post("/admin/cpfs", headers=admin_headers, follow_redirects=False, data={
            "nome": "JOAO SOUZA", "documento_publicado": "***.654.321-**", "cpf": "111.654.321-45",
            "fonte": "contrato social", "finalidade": "cadastro do socio no eSocial", "voltar": f"/socio/{chave}"})
        assert r.status_code == 303 and r.headers["location"] == f"/socio/{chave}"
        assert "111.654.321-45" in c.get(f"/socio/{chave}", headers=admin_headers).text
        # digitos que nao batem: recusado, com mensagem
        r = c.post("/admin/cpfs", headers=admin_headers, follow_redirects=False, data={
            "nome": "JOAO SOUZA", "documento_publicado": "***.654.321-**", "cpf": CPF_MARIA,
            "fonte": "contrato social", "finalidade": "cadastro do socio no eSocial", "voltar": f"/socio/{chave}"})
        assert "msg=" in r.headers["location"]

    def test_pagina_do_cofre_e_busca_por_cpf(self, base_local, admin_headers):
        from app.receita import jobs
        from app.services import cpf

        c = TestClient(app)
        assert c.get("/admin/cpfs", follow_redirects=False).status_code == 303  # exige login
        cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente",
                      documento_publicado="***123456**")
        r = c.get("/admin/cpfs", params={"cpf": "987.123.456-28"}, headers=admin_headers)
        assert r.status_code == 200
        assert "confirmada (cofre)" in r.text and "MARIA DA SILVA" in r.text
        titular = c.get("/admin/cpfs", params={"cpf": "12345678900"}, headers=admin_headers)
        assert titular.status_code == 400  # DV invalido: recusado
        busca = [a for a in jobs.auditoria() if a["acao"] == "cpf.buscar"]
        assert busca and CPF_MARIA not in json.dumps(busca, default=str)
        rid = cpf.listar()[0]["id"]
        assert c.post(f"/admin/cpfs/{rid}/remover", headers=admin_headers, follow_redirects=False).status_code == 303
        assert cpf.listar() == []

    def test_busca_acha_titular_do_mei(self, base_local, monkeypatch):
        from app.services import cpf

        # a base de teste tem o MEI com CPF de DV invalido; troca por um valido
        monkeypatch.setattr(cpf, "cpf_valido", lambda x: len(cpf.so_digitos(x)) == 11)
        r = cpf.buscar("12345678900")
        assert [e["cnpj"] for e in r["titular"]] == [MEI]


class TestFonteExterna:
    @respx.mock
    def test_cpf_completo_da_api_vai_para_o_cofre_e_resposta_publica_mascara(self, db_initialized, brasilapi_payload,
                                                                            admin_headers):
        from app.receita import jobs
        from app.services import cpf

        cnpj = "19131243000197"
        payload = brasilapi_payload | {"qsa": [brasilapi_payload["qsa"][0] | {"cnpj_cpf_do_socio": CPF_MARIA}]}
        nome = payload["qsa"][0]["nome_socio"]
        respx.get(url__regex=r".*minhareceita.*").mock(return_value=httpx.Response(200, json=payload))
        respx.get(url__regex=r".*brasilapi.*").mock(return_value=httpx.Response(200, json=payload))
        respx.get(url__regex=r".*receitaws.*").mock(return_value=httpx.Response(404, json={}))
        c = TestClient(app)
        api = c.get(f"/api/companies/{cnpj}").text
        assert CPF_MARIA not in api  # resposta publica continua mascarada
        reg = cpf.listar()
        assert reg and reg[0]["origem"] == "fonte_externa" and reg[0]["nome"] == nome
        assert [a for a in jobs.auditoria() if a["acao"] == "cpf.capturado"]
        assert "987.123.456-28" in c.get(f"/empresa/{cnpj}", headers=admin_headers).text


class TestFerramentas:
    async def test_ferramentas_de_cpf_so_com_admin(self, base_local):
        from app import mcp_server
        from app.chat import ferramentas
        from app.services import cpf

        mcp_server.configurar(admin=False)
        nomes = {t.name for t in await mcp_server.mcp.list_tools()}
        assert "cpf_completo_dos_socios" not in nomes and "buscar_por_cpf" not in nomes
        cpf.cadastrar("MARIA DA SILVA", CPF_MARIA, fonte="contrato social", finalidade="eSocial do cliente",
                      documento_publicado="***123456**")
        ok, txt = await ferramentas.executar("cpf_completo_dos_socios", {"cnpj": CONTABIL}, max_chars=10_000)
        assert ok, txt
        dados = json.loads(txt)
        maria = next(s for s in dados["socios"] if s["nome"] == "MARIA DA SILVA")
        assert maria["cpf_completo"] == "987.123.456-28"
        joao = next(s for s in dados["socios"] if s["nome"] == "JOAO SOUZA")
        assert joao["cpf_completo"] is None
        ok, txt = await ferramentas.executar("cpf_completo_dos_socios", {"cnpj": MEI}, max_chars=10_000)
        assert json.loads(txt)["cpf_titular"] == "123.456.789-00"
        mcp_server.configurar(admin=False)

    def test_exportacao_com_cpf_do_titular_exige_finalidade(self, base_local):
        c = TestClient(app)
        r = c.get("/api/busca/exportar.csv", params={"cnpj": MEI, "colunas": ["cnpj", "cpf_titular"]})
        assert r.status_code == 400
        r = c.get("/api/busca/exportar.csv", params={"cnpj": MEI, "colunas": ["cnpj", "cpf_titular"],
                                                       "finalidade": "cadastro do cliente no eSocial"})
        assert r.status_code == 200 and "12345678900" in r.content.decode("utf-8-sig")
