"""Regressao do Gauntlet Loop (rodada 1): consulta, provedores, API, CLI e paginas.
Cada teste reproduziu um defeito real e hoje guarda a correcao.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import ProviderResult
from app.schemas.company import CompanyUnified, PartnerPublic, SourceEntry
from app.services.diligence import build_diligence_checklist, build_diligence_verdict
from app.services.reconciliation import reconcile

CNPJ = "19131243000197"
MR = f"https://minhareceita.org/{CNPJ}"
BA = f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}"
RW = f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}"


def _mk(provider: str, raw: dict) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        raw=raw,
        http_status=200,
        fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
        is_mirror_of_rfb=provider != "minha_receita",
    )


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


# Q1 ---------------------------------------------------------------------------
@respx.mock
def test_q1_resposta_200_nao_json_nao_conta_como_fonte_ok_nem_vai_para_cache(client, brasilapi_payload):
    """Proxy/pagina de bloqueio devolve 200 com HTML: nao e 'OK' e nao pode envenenar o cache."""
    mr = respx.get(MR).mock(return_value=httpx.Response(200, text="<html>Acesso bloqueado</html>"))
    respx.get(BA).mock(return_value=httpx.Response(503, text="fora"))
    respx.get(RW).mock(return_value=httpx.Response(503, text="fora"))

    body = client.get(f"/api/companies/{CNPJ}").json()
    mr_entry = next(f for f in body["fontes"] if f["fonte"] == "minha_receita")

    # A fonte volta a funcionar: a nova consulta precisa ir as fontes, nao ao cache vazio.
    mr.mock(return_value=httpx.Response(200, json=brasilapi_payload))
    lote = client.get(f"/api/batch?cnpjs={CNPJ}").json()["resultados"][0]

    problemas = []
    if mr_entry["status"] == 200 and not mr_entry["erro"]:
        problemas.append(f"HTML contado como fonte OK: {mr_entry}")
    if lote["razao_social"] is None:
        problemas.append(f"cache envenenado: {lote['status']} / {lote['mensagem']} / do_cache={lote['do_cache']}")
    assert not problemas, problemas


# Q2 ---------------------------------------------------------------------------
def test_q2_situacao_especial_das_fontes_e_descartada():
    """Recuperacao judicial/falencia informada pela fonte some da consulta unificada."""
    raw = {
        "razao_social": "X LTDA",
        "situacao_cadastral": 2,
        "descricao_situacao_cadastral": "ATIVA",
        "situacao_especial": "RECUPERACAO JUDICIAL",
        "data_situacao_especial": "2025-01-01",
    }
    c = reconcile(CNPJ, [_mk("minha_receita", raw)])
    assert c.situacao_especial == "RECUPERACAO JUDICIAL"


# Q3 ---------------------------------------------------------------------------
def test_q3_socios_homonimos_com_documentos_diferentes_sao_fundidos(base_local):
    """Base local tem duas MARIA DA SILVA (***123456** e ***999888**): a API mostra uma so."""
    body = TestClient(app).get("/api/companies/87654321000198").json()
    docs = sorted(s["documento_mascarado"] for s in body["socios"])
    assert docs == ["***123456**", "***999888**"], body["socios"]


# Q4 ---------------------------------------------------------------------------
@respx.mock
def test_q4_cpf_de_socio_sem_mascara_vaza_na_api_e_no_csv(client, brasilapi_payload):
    """Se a fonte mandar o CPF inteiro, a consulta unificada o repassa (ficha.py mascara, aqui nao)."""
    payload = brasilapi_payload | {
        "qsa": [brasilapi_payload["qsa"][0] | {"cnpj_cpf_do_socio": "12345678901"}]
    }
    respx.get(MR).mock(return_value=httpx.Response(200, json=payload))
    respx.get(BA).mock(return_value=httpx.Response(200, json=payload))
    respx.get(RW).mock(return_value=httpx.Response(404, json={}))

    api = client.get(f"/api/companies/{CNPJ}").text
    csv_ = client.get(f"/api/companies/{CNPJ}/export.csv").content.decode("utf-8-sig")
    assert "12345678901" not in api
    assert "12345678901" not in csv_


# Q5 ---------------------------------------------------------------------------
def _empresa_limpa(**kw) -> CompanyUnified:
    agora = datetime(2026, 10, 1, tzinfo=UTC)
    base = dict(
        cnpj=CNPJ,
        cnpj_formatado="19.131.243/0001-97",
        razao_social="X LTDA",
        situacao_cadastral="ATIVA",
        capital_social=1000.0,
        socios=[PartnerPublic(nome="A", fonte="minha_receita")],
        fontes=[SourceEntry(fonte="minha_receita", status=200, data_consulta=agora)],
    )
    return CompanyUnified(**(base | kw))


def test_q5_simples_mei_altera_veredicto_de_regularidade():
    """Simples/MEI nao e prova de regularidade: nao pode decidir 'Cadastro ativo, sem ressalvas'."""
    com = build_diligence_verdict(build_diligence_checklist(_empresa_limpa(opcao_simples=False, opcao_mei=False)))
    sem = build_diligence_verdict(build_diligence_checklist(_empresa_limpa()))
    item = next(i for i in build_diligence_checklist(_empresa_limpa(opcao_simples=True)) if i.id == "simples_mei")
    assert com.nivel == sem.nivel, (com.nivel, sem.nivel)
    assert item.status != "ok", item  # status "ok" e exibido como "Regular"


# Q6 ---------------------------------------------------------------------------
def test_q6_favoritar_em_paralelo_gera_500(db_initialized):
    """Duplo clique em 'Favoritar' (2 POSTs simultaneos) -> IntegrityError -> 500."""
    import app.services.history_service as hs
    from app.db import get_session_factory

    real = get_session_factory()
    barreira = threading.Barrier(2)

    class _Sessao:
        """Sincroniza as duas requisicoes entre o SELECT e o INSERT (janela real da corrida)."""

        def __init__(self):
            self.s = real()

        def __enter__(self):
            self.s.__enter__()
            orig = self.s.add

            def add(obj):
                barreira.wait(timeout=5)
                return orig(obj)

            self.s.add = add
            return self.s

        def __exit__(self, *a):
            return self.s.__exit__(*a)

    import app.db as db

    c = TestClient(app, raise_server_exceptions=False)
    codigos: list[int] = []
    orig_factory = db.get_session_factory
    db.get_session_factory = lambda: _Sessao  # usado por add_favorite via import local
    try:
        ts = [
            threading.Thread(target=lambda: codigos.append(c.post(f"/api/favorites/{CNPJ}").status_code))
            for _ in range(2)
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
    finally:
        db.get_session_factory = orig_factory
    assert hs.is_favorite(CNPJ)
    assert codigos == [200, 200], codigos


# Q7 ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [["consulta", "historico"], ["limpar-historico"], ["limpar-cache"]],
)
def test_q7_cli_documentada_quebra_sem_init_db(args):
    """README: `python -m app.cli limpar-historico` etc. Num banco ainda nao criado: traceback."""
    from typer.testing import CliRunner

    from app.cli import app as cli

    r = CliRunner().invoke(cli, args)
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)


# Q8 ---------------------------------------------------------------------------
def test_q8_cpf_do_empresario_individual_vai_para_buscadores(base_local, monkeypatch):
    """Razao social de EI/MEI traz o CPF do titular ('JOSE PEREIRA 12345678900', fixture do projeto):
    a 'pesquisa na internet' manda o CPF inteiro a Google/Jusbrasil/Brave."""
    from app.config import get_settings

    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "k")
    get_settings.cache_clear()
    c = TestClient(app)
    with respx.mock(assert_all_called=False) as router:
        brave = router.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(200, json={"web": {"results": []}})
        )
        links = c.get("/api/companies/11222333000181/research").json()
        c.get("/api/companies/11222333000181/web-search")
    urls = [lk["url"] for g in links["grupos"] for lk in g["links"]]
    enviado = str(brave.calls[0].request.url) if brave.calls else ""
    vazamentos = [u for u in urls if "12345678900" in u]
    if "12345678900" in enviado:
        vazamentos.append("brave:" + enviado)
    assert not vazamentos, vazamentos


# Q10 --------------------------------------------------------------------------
@pytest.mark.parametrize("nome", ["MERCADO CENTRAL", "Padaria Sao Joao", "CONTA FACIL LTDA"])
def test_q10_nome_com_14_letras_vira_cnpj_invalido_na_busca_inicial(base_local, nome):
    """Com a base local, o campo da pagina inicial aceita 'CNPJ, razao social ou nome fantasia'.
    Nome cujas letras somam 14 caracteres e tratado como CNPJ e recusado (400)."""
    r = TestClient(app).get("/consulta", params={"cnpj": nome}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/busca"), (r.status_code, nome)


# Q9 ---------------------------------------------------------------------------
@respx.mock
def test_q9_favoritar_e_apagar_historico_nao_funcionam_sem_javascript(client, brasilapi_payload):
    """pages.py: 'Todas as paginas funcionam sem JavaScript'. Favoritar/Remover/Apagar sao
    <button type=button> sem <form>: sem JS nao fazem nada."""
    respx.get(MR).mock(return_value=httpx.Response(200, json=brasilapi_payload))
    respx.get(BA).mock(return_value=httpx.Response(200, json=brasilapi_payload))
    respx.get(RW).mock(return_value=httpx.Response(404, json={}))
    html = client.get(f"/empresa/{CNPJ}").text
    client.post(f"/api/favorites/{CNPJ}?label=X")
    hist = client.get("/historico").text

    def em_form(doc: str, marcador: str) -> bool:
        i = doc.find(marcador)
        assert i >= 0, marcador
        antes = doc[:i]
        return antes.rfind("<form") > antes.rfind("</form>")

    casos = (
        (html, "data-favorite-url"),
        (hist, 'data-delete-url="/api/favorites'),
        (hist, 'data-delete-url="/api/history"'),
    )
    sem_js = [m for doc, m in casos if not em_form(doc, m)]
    assert not sem_js, sem_js


def test_q9_formularios_sem_javascript_gravam_e_voltam(client):
    """Os formularios em volta dos botoes (sem JS) gravam de fato e voltam para a pagina."""
    from app.services.history_service import is_favorite, recent_queries, record_query

    r = client.post(f"/favoritos/{CNPJ}", data={"acao": "adicionar", "rotulo": "X"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/empresa/{CNPJ}"
    assert is_favorite(CNPJ)
    r = client.post(f"/favoritos/{CNPJ}", data={"acao": "remover", "voltar": "//evil.example"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/empresa/{CNPJ}"  # sem redirecionamento aberto
    assert not is_favorite(CNPJ)
    record_query(cnpj=CNPJ, razao_social="X", situacao="ATIVA", sources="")
    assert client.post("/historico/limpar", follow_redirects=False).status_code == 303
    assert recent_queries() == []
    assert client.post("/favoritos/123", data={"acao": "adicionar"}).status_code == 400
