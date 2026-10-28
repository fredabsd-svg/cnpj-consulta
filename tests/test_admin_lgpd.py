"""Area administrativa (autorizacao, auditoria, tarefas), contatos
complementares, ficha cadastral e protecoes de dados pessoais (LGPD)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest
import respx
from fastapi.testclient import TestClient

from app.main import SemQueryString, app
from app.receita import jobs, servico
from app.services import contatos as ct
from app.services.ficha import ficha_local, mascarar_documento
from tests.receita_fake import PortalFalso, dataset_padrao

CNPJ = "12345678000195"
MEI = "11222333000181"
TOKEN = "token-de-teste-123456"


# ---------------------------------------------------------------- autorizacao


class TestAutorizacao:
    ROTAS = [
        ("get", "/api/admin/base/status", None),
        ("post", "/api/admin/base/verificar", None),
        ("post", "/api/admin/base/atualizar", {}),
        ("post", "/api/admin/base/restaurar", {"confirmar": "CONFIRMAR"}),
        ("get", "/api/admin/auditoria", None),
        ("post", f"/api/admin/contatos/{CNPJ}", {"tipo": "email", "valor": "a@b.com", "fonte": "site"}),
    ]

    @pytest.mark.parametrize("metodo,url,corpo", ROTAS)
    def test_rotas_admin_exigem_token(self, db_initialized, metodo, url, corpo):
        c = TestClient(app)
        r = getattr(c, metodo)(url, **({"json": corpo} if corpo is not None else {}))
        assert r.status_code == 401
        r = getattr(c, metodo)(url, headers={"Authorization": "Bearer errado"}, **({"json": corpo} if corpo is not None else {}))
        assert r.status_code == 401

    def test_paginas_admin_redirecionam_para_login(self, db_initialized):
        c = TestClient(app)
        for url in ("/base", "/admin/auditoria"):
            r = c.get(url, follow_redirects=False)
            assert r.status_code == 303 and "/admin/entrar" in r.headers["location"]
        r = c.post(f"/admin/contatos/{CNPJ}", data={"tipo": "email"}, follow_redirects=False)
        assert r.status_code == 303 and "/admin/entrar" in r.headers["location"]

    def test_login_cookie_logout_e_auditoria(self, db_initialized):
        c = TestClient(app)
        r = c.post("/admin/entrar", data={"token": "errado", "proximo": "/base"})
        assert r.status_code == 401
        r = c.post("/admin/entrar", data={"token": TOKEN, "proximo": "//evil.com"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/base"  # sem redirecionamento aberto
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert c.get("/api/admin/base/status").status_code == 200
        c.post("/admin/sair")
        assert c.get("/api/admin/base/status").status_code == 401
        acoes = [(a["acao"], a["resultado"]) for a in jobs.auditoria()]
        assert ("admin.login", "negado") in acoes and ("admin.login", "ok") in acoes

    def test_bloqueio_apos_tentativas(self, db_initialized):
        c = TestClient(app)
        for _ in range(5):
            assert c.post("/admin/entrar", data={"token": "x"}).status_code == 401
        assert c.post("/admin/entrar", data={"token": TOKEN}).status_code == 429

    def test_sessao_adulterada_ou_expirada(self, db_initialized):
        from app.services import admin_auth

        s = admin_auth.criar_sessao()
        assert admin_auth.sessao_valida(s)
        exp, nonce, sig = s.split(".")
        assert not admin_auth.sessao_valida(f"{int(exp) + 9999}.{nonce}.{sig}")
        assert not admin_auth.sessao_valida(admin_auth.criar_sessao(agora=0))

    def test_token_gerado_em_arquivo_privado(self, db_initialized, monkeypatch, tmp_path):
        monkeypatch.setenv("ADMIN_TOKEN", "")
        from app.config import get_settings
        from app.services import admin_auth

        get_settings.cache_clear()
        t = admin_auth.token_admin()
        assert len(t) >= 16 and admin_auth.token_admin() == t
        arq = admin_auth._arquivo_token()
        assert (arq.stat().st_mode & 0o077) == 0

    def test_post_de_outra_origem_bloqueado(self, db_initialized):
        r = TestClient(app).post("/admin/entrar", data={"token": TOKEN}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403

    def test_destrutivas_pedem_confirmacao(self, base_local, admin_headers):
        c = TestClient(app)
        r = c.post("/api/admin/base/restaurar", json={}, headers=admin_headers)
        assert r.status_code == 400 and "CONFIRMAR" in r.json()["detail"]
        r = c.post("/api/admin/base/limpar", json={"confirmar": "sim"}, headers=admin_headers)
        assert r.status_code == 400


# ---------------------------------------------------------------- tarefas


class TestTarefas:
    def test_atualizacao_pela_api_com_pausa_e_retomada(self, db_initialized, admin_headers, monkeypatch):
        """Fluxo do painel: cria tarefa, pausa, retoma, conclui (processo de trabalho
        executado aqui mesmo em vez de num subprocesso)."""
        iniciados: list[int] = []
        monkeypatch.setattr(jobs, "iniciar_processo", lambda job_id, log_dir: iniciados.append(job_id) or 0)
        c = TestClient(app)
        portal = PortalFalso({"2026-10": dataset_padrao()})
        with respx.mock(assert_all_called=False) as router:
            portal.instalar(router)
            router.route(host="testserver").pass_through()
            r = c.post("/api/admin/base/atualizar", json={"mes": "2026-10"}, headers=admin_headers)
            assert r.status_code == 202
            job_id = r.json()["id"]
            assert iniciados == [job_id]
            # segunda tarefa simultanea e recusada
            assert c.post("/api/admin/base/atualizar", json={}, headers=admin_headers).status_code == 409
            c.post(f"/api/admin/base/job/{job_id}/pausar", headers=admin_headers, json={})
            assert servico.executar_job(job_id, sleep=lambda s: None) == "pausado"
            st = c.get(f"/api/admin/base/job/{job_id}", headers=admin_headers).json()
            assert st["status"] == "pausado" and st["logs"]
            assert c.post(f"/api/admin/base/job/{job_id}/retomar", headers=admin_headers, json={}).status_code == 200
            assert servico.executar_job(job_id, sleep=lambda s: None) == "concluido"
        st = c.get("/api/admin/base/status", headers=admin_headers).json()
        assert st["status"] in ("atualizada",) and st["ativa"]["mes"] == "2026-10"
        assert st["ativa"]["contagens"]["estabelecimentos"] == 4
        acoes = {a["acao"] for a in jobs.auditoria()}
        assert {"base.atualizar", "base.pausar", "base.retomar"} <= acoes

    def test_cancelamento_nao_altera_base_ativa(self, base_local, admin_headers, monkeypatch):
        monkeypatch.setattr(jobs, "iniciar_processo", lambda job_id, log_dir: 0)
        c = TestClient(app)
        job_id = c.post("/api/admin/base/atualizar", json={}, headers=admin_headers).json()["id"]
        assert c.post(f"/api/admin/base/job/{job_id}/cancelar", json={}, headers=admin_headers).status_code == 400
        assert c.post(f"/api/admin/base/job/{job_id}/cancelar", json={"confirmar": "CONFIRMAR"},
                      headers=admin_headers).status_code == 200
        assert servico.executar_job(job_id) == "cancelado"
        from app.receita.armazenamento import base_ativa

        assert base_ativa().caminho == base_local

    def test_falha_e_retomavel_e_heartbeat(self, db_initialized):
        job = jobs.criar_job("atualizar", "2026-10")
        rep = jobs.JobReporter(job["id"])
        rep.iniciar()
        from app.db import get_session_factory
        from app.models.base_cnpj import BaseJob

        with get_session_factory()() as s:
            j = s.get(BaseJob, job["id"])
            j.heartbeat_em = datetime.now() - timedelta(minutes=10)
            s.commit()
        assert jobs.obter(job["id"])["status_efetivo"] == "interrompido"
        assert jobs.job_ativo() is None
        assert jobs.preparar_retomada(job["id"])["status"] == "pendente"

    def test_validacao_da_base_ativa(self, base_local):
        job = servico.iniciar_validacao(em_processo=False)
        assert servico.executar_job(job["id"]) == "concluido"
        rel = jobs.obter(job["id"])["relatorio"]
        assert rel["contagens"]["empresas"] == 3 and not rel["erros"]

    def test_agendamento(self, db_initialized, monkeypatch):
        chamadas = []
        monkeypatch.setattr(servico, "verificar_atualizacao",
                            lambda client=None: {"mes_remoto": "2026-10", "nova_versao": True, "erro": None})
        monkeypatch.setattr(servico, "iniciar_atualizacao",
                            lambda mes, origem, em_processo: chamadas.append((mes, origem)) or {"id": 1})
        assert servico._tick_agenda() is None  # desligado
        ontem = datetime(2026, 10, 27, 12, 0)
        jobs.config_set(servico.CFG_AGENDA, {"ativo": True, "hora": 3, "dia_semana": None,
                                             "ultima_execucao": ontem.isoformat()})
        assert servico._tick_agenda(agora=datetime(2026, 10, 28, 2, 0)) is None  # antes das 3h
        assert servico._tick_agenda(agora=datetime(2026, 10, 28, 3, 5)) == "iniciada"
        assert chamadas == [("2026-10", "agendada")]
        assert servico._tick_agenda(agora=datetime(2026, 10, 28, 4, 0)) is None  # ja rodou hoje


# ---------------------------------------------------------------- ficha


class TestFicha:
    def test_campos_existentes_na_fonte(self, base_local):
        f = ficha_local(CNPJ)
        i = f["identificacao"]
        assert i["razao_social"] == "CONTABIL TESTE LTDA" and i["natureza"] == "Sociedade Empresária Limitada"
        assert i["porte"] == "MICRO EMPRESA" and i["capital_social"] == 15000.0
        assert i["simples"]["optante"] and i["mei"]["optante"] is False
        assert f["endereco"]["municipio_nome"] == "PALMAS" and f["endereco"]["mapa_url"].startswith("https://www.openstreetmap.org/")
        assert f["cnaes"]["principal"]["descricao"] == "Atividades de contabilidade"
        assert [c["codigo"] for c in f["cnaes"]["secundarios"]] == [6920602, 8211300]
        assert f["estabelecimentos"]["total"] == 2  # matriz + filial
        assert f["base"]["mes"] == "2026-10"
        assert any("Opção pelo Simples" in e["evento"] for e in f["historico"])

    def test_empresa_sem_telefone_email_e_sem_qsa(self, base_local):
        f = ficha_local(MEI)
        assert f["contatos_oficiais"] == [] and f["socios"] == []
        assert f["identificacao"]["mei"]["optante"]
        html = TestClient(app).get(f"/empresa/{MEI}").text
        assert "não declarou telefone nem e-mail" in html and "não traz quadro societário" in html

    def test_cnpj_invalido_e_inexistente(self, base_local):
        c = TestClient(app)
        assert c.get("/api/base/empresa/123").status_code == 400
        assert c.get("/api/base/empresa/11444777000161").status_code == 404
        assert ficha_local("11444777000161") is None

    def test_consulta_local_primeiro_sem_fontes_externas(self, base_local):
        with respx.mock(assert_all_called=False) as router:
            externo = router.get(url__regex=r"https://(minhareceita|brasilapi|www\.receitaws).*").mock(
                side_effect=AssertionError("nao deveria chamar fonte externa"))
            r = TestClient(app).get(f"/empresa/{CNPJ}")
        assert r.status_code == 200 and not externo.called
        assert "Comparar com as fontes externas" in r.text

    def test_alteracoes_entre_versoes(self, db_initialized, monkeypatch):
        from app.receita.pipeline import Atualizacao
        from tests.receita_fake import csv_bytes, estab, zip_bytes

        monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
        d1 = dataset_padrao()
        d2 = dataset_padrao()
        d2["Estabelecimentos0.zip"] = zip_bytes("K.ESTABELE", csv_bytes([
            estab("12345678", "0001", "95", fantasia="CONTA FACIL", situacao="08", ddd1="63", tel1="32151234",
                  email="novo@contafacil.com.br"),
            estab("87654321", "0001", "98", cnae="4711302", municipio="9241", email="mercado@gmail.com"),
        ]))
        with respx.mock(assert_all_called=False) as router:
            PortalFalso({"2026-09": d1, "2026-10": d2}).instalar(router)
            Atualizacao("2026-09", sleep=lambda s: None).executar()
            Atualizacao("2026-10", sleep=lambda s: None, forcar=True).executar()
        alt = ficha_local(CNPJ)["alteracoes"]
        campos = {m["campo"]: m for m in alt["mudancas"]}
        assert alt["mes_anterior"] == "2026-09"
        assert campos["Situação cadastral"]["antes"] == "ATIVA" and campos["Situação cadastral"]["depois"] == "BAIXADA"
        # contato mudou, mas o valor nao e exibido no comparativo
        assert "novo@" not in str(campos["E-mail"])

    def test_pagina_do_socio_e_xlsx(self, base_local):
        chave = next(s["chave"] for s in ficha_local(CNPJ)["socios"] if s["nome"] == "MARIA DA SILVA")
        c = TestClient(app)
        assert "identificação provável" in c.get(f"/socio/{chave}").text
        assert c.get("/socio/nao-existe").status_code == 404
        r = c.get(f"/empresa/{CNPJ}/ficha.xlsx")
        assert r.status_code == 200 and r.content[:2] == b"PK"


# ---------------------------------------------------------------- contatos complementares


class TestContatosComplementares:
    def test_cadastro_exige_fonte_e_nunca_e_automatico(self, base_local, admin_headers):
        c = TestClient(app)
        assert ct.listar(CNPJ) == []  # nada inferido
        r = c.post(f"/api/admin/contatos/{CNPJ}", headers=admin_headers,
                   json={"tipo": "telefone", "valor": "(63) 98888-7777", "fonte": ""})
        assert r.status_code == 400 and "fonte" in r.json()["detail"]
        r = c.post(f"/api/admin/contatos/{CNPJ}", headers=admin_headers,
                   json={"tipo": "telefone", "valor": "(63) 98888-7777", "fonte": "Site oficial da empresa",
                         "url_fonte": "https://contafacil.com.br/contato", "verificado": True})
        assert r.status_code == 201
        k = r.json()
        assert k["subtipo"] == "celular" and k["valor_normalizado"] == "63988887777"
        assert k["data_verificacao"] == date.today().isoformat() and k["difere_do_oficial"]
        # duplicado recusado
        assert c.post(f"/api/admin/contatos/{CNPJ}", headers=admin_headers,
                      json={"tipo": "telefone", "valor": "63988887777", "fonte": "outra"}).status_code == 400
        # aparece separado dos oficiais na pagina
        html = c.get(f"/empresa/{CNPJ}").text
        assert "Contatos complementares" in html and "Site oficial da empresa" in html

    def test_filtros_de_contato_complementar_e_remocao(self, base_local, admin_headers):
        from app.services import busca_avancada as ba

        c = TestClient(app)
        k = c.post(f"/api/admin/contatos/{MEI}", headers=admin_headers,
                   json={"tipo": "site", "valor": "www.josepereira.com.br/", "fonte": "Cartão de visita"}).json()
        assert k["valor_normalizado"] == "josepereira.com.br" and not k["verificado"]
        achar = lambda p: [i["cnpj"] for i in ba.buscar(ba.Filtros.de_params(p)).itens]  # noqa: E731
        assert achar({"tem_site": "sim"}) == [MEI]
        assert achar({"comp_verificado": "nao"}) == [MEI]
        assert achar({"origem_contato": "complementar"}) == [MEI]
        assert MEI not in achar({"origem_contato": "oficial"})
        assert MEI not in achar({"comp_tem": "nao"})
        r = c.put(f"/api/admin/contatos/{MEI}/{k['id']}", headers=admin_headers,
                  json={"tipo": "site", "valor": "josepereira.com.br", "fonte": "Cartão de visita",
                        "verificado": True, "data_verificacao": "2026-10-01"})
        assert r.status_code == 200 and r.json()["verificado"]
        assert achar({"comp_verificado": "sim", "comp_verificado_desde": "2026-09-01"}) == [MEI]
        assert c.delete(f"/api/admin/contatos/{MEI}/{k['id']}", headers=admin_headers).status_code == 204
        assert achar({"tem_site": "sim"}) == []
        det = [a["detalhe"] for a in jobs.auditoria() if a["acao"].startswith("contato.")]
        assert det and not any("josepereira" in d for d in det)  # auditoria sem o valor

    @pytest.mark.parametrize("tipo,valor", [("telefone", "123"), ("email", "nao-e-email"), ("site", "ftp://x"),
                                            ("telefone", "(01) 3333-4444"), ("fax", "x")])
    def test_validacao(self, db_initialized, tipo, valor):
        with pytest.raises(ct.ContatoInvalido):
            ct.salvar(CNPJ, tipo=tipo, valor=valor, fonte="teste")

    def test_classificacao_telefone(self):
        assert ct.classificar_telefone("63992345678") == "celular"
        assert ct.classificar_telefone("6332151234") == "fixo"
        assert ct.classificar_telefone("6392345678") == "celular"  # como a Receita publica: sem o nono digito
        assert ct.classificar_telefone("6312345678") == "indeterminado"

    def test_retencao(self, db_initialized):
        ct.salvar(CNPJ, tipo="email", valor="velho@x.com.br", fonte="planilha antiga", verificado=True,
                  data_verificacao=date.today() - timedelta(days=1000))
        ct.salvar(CNPJ, tipo="email", valor="novo@x.com.br", fonte="site", verificado=True)
        assert ct.descartar_por_retencao() == 1
        assert [k["valor"] for k in ct.listar(CNPJ)] == ["novo@x.com.br"]

    def test_formulario_sem_js(self, base_local):
        c = TestClient(app)
        c.post("/admin/entrar", data={"token": TOKEN})
        r = c.post(f"/admin/contatos/{CNPJ}", data={"tipo": "email", "valor": "x", "fonte": "site"}, follow_redirects=False)
        assert r.status_code == 303 and "erro_contato" in r.headers["location"]
        r = c.post(f"/admin/contatos/{CNPJ}", data={"tipo": "email", "valor": "a@contafacil.com.br", "fonte": "site"},
                   follow_redirects=False)
        assert r.status_code == 303 and "erro" not in r.headers["location"]
        assert ct.listar(CNPJ)[0]["valor"] == "a@contafacil.com.br"


# ---------------------------------------------------------------- LGPD


class TestDadosPessoais:
    def test_mascaramento(self):
        assert mascarar_documento("***123456**", 2) == "***.123.456-**"
        assert mascarar_documento("12345678901", 2) == "***.456.789-**"  # nunca exibe CPF completo
        assert mascarar_documento("12345678000195", 1) == "12.345.678/0001-95"  # CNPJ de socio PJ e publico
        assert mascarar_documento("***000000**", 3) is None
        assert mascarar_documento(None, 2) is None

    def test_cpf_nunca_aparece_completo_na_pagina(self, base_local):
        html = TestClient(app).get(f"/empresa/{CNPJ}").text
        assert "***.123.456-**" in html and "***123456**" not in html

    def test_logs_de_acesso_sem_query_string(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("127.0.0.1", "GET", "/busca?socio_nome=MARIA+DA+SILVA", "1.1", 200), None)
        SemQueryString().filter(rec)
        assert "MARIA" not in rec.getMessage() and "/busca?" in rec.getMessage()

    def test_historico_de_buscas_nao_guarda_nome_de_socio(self, base_local):
        from app.services import listas

        TestClient(app).get("/api/busca?socio_nome=maria&uf=TO")
        rec = listas.recentes()
        assert rec and "socio_nome" not in rec[0]["filtros"] and "MARIA" not in rec[0]["resumo"].upper()

    def test_logs_da_importacao_sem_dados_pessoais(self, db_initialized):
        job = jobs.criar_job("atualizar", "2026-10")
        with respx.mock(assert_all_called=False) as router:
            PortalFalso({"2026-10": dataset_padrao()}).instalar(router)
            assert servico.executar_job(job["id"], sleep=lambda s: None) == "concluido"
        texto = " ".join(x["mensagem"] for x in jobs.logs(job["id"], 500))
        for pessoal in ("MARIA", "JOAO", "123456", "contafacil"):
            assert pessoal not in texto
