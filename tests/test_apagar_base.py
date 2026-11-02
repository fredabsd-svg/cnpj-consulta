"""Apagar a base completa: so o que o app criou (a pasta pode ser a raiz de um HD
externo), opcionalmente os dados do usuario no app, sempre auditado."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app


def _dados_do_app():
    from app.chat import provedores
    from app.services import cpf, listas

    lista = listas.criar_lista("Clientes", None)
    cpf.cadastrar("MARIA DA SILVA", "98712345628", fonte="contrato social", finalidade="eSocial do cliente")
    provedores.salvar("groq", api_key="gsk_chave_teste_123456")
    return lista


def test_apaga_base_e_temporarios_sem_tocar_em_outros_arquivos(base_local, admin_headers, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.receita import armazenamento as arm
    from app.receita import jobs
    from app.services import listas

    temp = tmp_path / "ssd"
    monkeypatch.setenv("RECEITA_TEMP_DIR", str(temp))
    get_settings.cache_clear()
    raiz = arm.data_dir()
    (raiz / "meus_documentos.txt").write_text("nao apagar")  # algo do usuario no mesmo disco
    (raiz / "trabalho" / "2026-11").mkdir(parents=True)
    (raiz / "trabalho" / "2026-11" / "bruto.duckdb").write_bytes(b"x" * 1000)
    (temp / "cnpj-consulta" / "2026-11").mkdir(parents=True)
    (temp / "cnpj-consulta" / "2026-11" / "a.csv").write_text("x")
    (temp / "outra_coisa.txt").write_text("nao apagar")
    lista = _dados_do_app()

    c = TestClient(app)
    assert c.post("/api/admin/base/apagar", headers=admin_headers, json={"confirmar": "CONFIRMAR"}).status_code == 400
    r = c.post("/api/admin/base/apagar", headers=admin_headers, json={"confirmar": "apagar tudo"})
    assert r.status_code == 200, r.text
    assert r.json()["liberado"] > 0 and r.json()["falhas"] == []

    assert arm.base_ativa() is None
    assert not (raiz / "versoes").exists() and not (raiz / "trabalho").exists() and not (raiz / arm.PONTEIRO).exists()
    assert not (temp / "cnpj-consulta").exists()
    assert (raiz / "meus_documentos.txt").read_text() == "nao apagar"
    assert (temp / "outra_coisa.txt").exists()
    # sem a opcao, os dados do app ficam
    assert listas.obter_lista(lista["id"]) is not None
    assert any(a["acao"] == "base.apagar_tudo" and "incluir_app=False" in a["detalhe"] for a in jobs.auditoria())
    assert c.get("/base", headers=admin_headers).status_code == 200  # painel abre sem base instalada


def test_opcao_apaga_tambem_os_dados_do_app(base_local, admin_headers):
    from app.chat import provedores
    from app.db import get_session_factory
    from app.models.base_cnpj import ListaEmpresas, SocioCpf
    from app.receita import jobs

    _dados_do_app()
    r = TestClient(app).post("/api/admin/base/apagar", headers=admin_headers,
                             json={"confirmar": "APAGAR TUDO", "incluir_app": True})
    assert r.status_code == 200
    assert r.json()["registros_apagados"]["lista_empresas"] == 1 and r.json()["registros_apagados"]["socio_cpf"] == 1
    with get_session_factory()() as s:
        assert not s.scalars(select(ListaEmpresas)).all() and not s.scalars(select(SocioCpf)).all()
    assert provedores.resolver("groq").configurado  # configuracao fica
    # a auditoria fica (registra a propria exclusao)
    assert any(a["acao"] == "base.apagar_tudo" and "incluir_app=True" in a["detalhe"] for a in jobs.auditoria())


def test_tarefa_interrompida_vira_cancelada_e_tarefa_ativa_bloqueia(db_initialized, admin_headers):
    from app.db import get_session_factory
    from app.models.base_cnpj import BaseJob
    from app.receita import jobs

    job = jobs.criar_job("atualizar", "2026-10")
    with get_session_factory()() as s:
        s.get(BaseJob, job["id"]).status = "falhou"
        s.commit()
    c = TestClient(app)
    assert c.post("/api/admin/base/apagar", headers=admin_headers, json={"confirmar": "APAGAR TUDO"}).status_code == 200
    assert jobs.obter(job["id"])["status"] == "cancelado"

    ativo = jobs.criar_job("atualizar", "2026-10")
    with get_session_factory()() as s:
        j = s.get(BaseJob, ativo["id"])
        j.status, j.heartbeat_em = "executando", __import__("datetime").datetime.now()
        s.commit()
    r = c.post("/api/admin/base/apagar", headers=admin_headers, json={"confirmar": "APAGAR TUDO"})
    assert r.status_code == 409


def test_formulario_sem_javascript_exige_texto(db_initialized, admin_headers):
    c = TestClient(app)
    r = c.post("/base/acao", headers=admin_headers, data={"acao": "apagar_tudo", "confirmar": "CONFIRMAR"},
               follow_redirects=False)
    assert "APAGAR+TUDO" in r.headers["location"] or "APAGAR%20TUDO" in r.headers["location"]
    r = c.post("/base/acao", headers=admin_headers, data={"acao": "apagar_tudo", "confirmar": "APAGAR TUDO"},
               follow_redirects=False)
    assert "Base+apagada" in r.headers["location"]


def test_limpar_temporarios_inclui_a_pasta_de_temporarios_em_outro_disco(db_initialized, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.receita import armazenamento as arm
    from app.receita import servico

    temp = tmp_path / "ssd"
    monkeypatch.setenv("RECEITA_TEMP_DIR", str(temp))
    get_settings.cache_clear()
    (temp / "cnpj-consulta" / "2026-11").mkdir(parents=True)
    (temp / "cnpj-consulta" / "2026-11" / "a.csv").write_text("x" * 10)
    (temp / "outra_coisa.txt").write_text("fica")
    (arm.data_dir() / "trabalho").mkdir(parents=True, exist_ok=True)
    assert servico.limpar_trabalho() == 10
    assert not (temp / "cnpj-consulta").exists() and (temp / "outra_coisa.txt").exists()
