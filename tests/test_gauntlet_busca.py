"""Regressao do Gauntlet Loop (rodada 1): busca avancada, exportacao, listas, LGPD
e MCP. Cada teste reproduziu um defeito real e hoje guarda a correcao.
"""

from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient
from mcp import Client

from app import mcp_server
from app.core import cnpj_validator as cv
from app.main import app
from app.services import busca_avancada as ba
from tests import receita_fake as rf

CONTABIL = "12345678000195"
FILIAL = "12345678000276"
MERCADO = "87654321000198"
MEI = "11222333000181"


def cn(params: dict) -> list[str]:
    return sorted(i["cnpj"] for i in ba.buscar(ba.Filtros.de_params(params)).itens)


def _dv(base12: str) -> str:
    d1 = cv._calc_dv(base12, cv._WEIGHTS_DV1)
    d2 = cv._calc_dv(base12 + str(d1), cv._WEIGHTS_DV2)
    return f"{d1}{d2}"


ALFA_BASE, ALFA_ORDEM = "12ABC345", "01DE"
ALFA = ALFA_BASE + ALFA_ORDEM + _dv(ALFA_BASE + ALFA_ORDEM)  # 12ABC34501DE35 (DV valido)


@pytest.fixture
def base_alfa(monkeypatch, db_initialized):
    """Base padrao + uma empresa com CNPJ alfanumerico (IN RFB 2.229/2024)."""
    from app.config import get_settings
    from app.providers.registry import get_registry
    from app.receita.armazenamento import base_ativa, limpar_cache
    from app.receita.pipeline import Atualizacao

    monkeypatch.setenv("RECEITA_LOCAL_ENABLED", "true")
    get_settings.cache_clear()
    get_registry.cache_clear()
    files = rf.dataset_padrao()
    files["Empresas2.zip"] = rf.zip_bytes("K3241.K03200Y2.D61010.EMPRECSV", rf.csv_bytes([
        [ALFA_BASE, "ALFA NOVA LTDA", "2062", "49", "1000,00", "01", ""]]))
    files["Estabelecimentos2.zip"] = rf.zip_bytes("K3241.K03200Y2.D61010.ESTABELE", rf.csv_bytes([
        rf.estab(ALFA_BASE, ALFA_ORDEM, ALFA[-2:], uf="SP", municipio="7107")]))
    with respx.mock(assert_all_called=False) as router:
        rf.PortalFalso({"2026-10": files}).instalar(router)
        Atualizacao(sleep=lambda s: None).executar()
    limpar_cache()
    return base_ativa().caminho


# B1 ------------------------------------------------------------------ NAO x NULL


def test_b1_excluir_nao_descarta_quem_nao_tem_o_dado(base_local):
    """NAO(condicao) sobre coluna NULL vira NULL e a linha some (logica de 3 valores)."""
    sem_ddd_63 = sorted([FILIAL, MERCADO, MEI])
    neg_ddd = json.dumps({"op": "E", "negar": True, "itens": [{"campo": "ddd", "valor": ["63"]}]})
    obtido = {
        "excluir_ddd=63": cn({"excluir_ddd": ["63"]}),
        "excluir_tipo_telefone=celular": cn({"excluir_tipo_telefone": ["celular"]}),
        "excluir_fantasia=conta": cn({"excluir_fantasia": "conta"}),
        "excluir_q=conta facil": cn({"excluir_q": "conta facil"}),
        "excluir_email_tipo=webmail": cn({"excluir_email_tipo": "webmail"}),
        "excluir_bairro=centro": cn({"excluir_bairro": "centro"}),
        "arvore NAO ddd=63": cn({"arvore": neg_ddd}),
    }
    esperado = {
        "excluir_ddd=63": sem_ddd_63,
        "excluir_tipo_telefone=celular": sem_ddd_63,
        "excluir_fantasia=conta": sem_ddd_63,
        "excluir_q=conta facil": sem_ddd_63,
        "excluir_email_tipo=webmail": sorted([CONTABIL, FILIAL, MEI]),
        "excluir_bairro=centro": [FILIAL],
        "arvore NAO ddd=63": sem_ddd_63,
    }
    assert obtido == esperado


# B2 ------------------------------------------------------------------ LGPD historico/exportacoes


def test_b2_historico_e_exportacoes_guardam_dados_de_socio(base_local, monkeypatch):
    from app.config import get_settings
    from app.services import listas

    c = TestClient(app)
    arv = json.dumps({"op": "E", "itens": [{"campo": "socio_nome", "valor": "maria da silva"}]})
    assert c.get("/api/busca", params={"arvore": arv}).status_code == 200
    assert c.get("/api/busca", params={"excluir_socio_documento": "***654321**", "uf": "TO"}).status_code == 200
    gravado = json.dumps([r["filtros"] for r in listas.recentes()], ensure_ascii=False).lower()
    html = c.get("/busca").text.lower()

    monkeypatch.setenv("EXPORTACAO_SINCRONA_MAX", "0")  # forca exportacao em segundo plano
    get_settings.cache_clear()
    d = c.get("/api/busca/exportar.csv", params={"socio_nome": "joao souza"}).json()
    assert d["segundo_plano"]
    publico = json.dumps(c.get("/api/exportacoes").json(), ensure_ascii=False).lower()

    assert "maria da silva" not in gravado, gravado
    assert "654321" not in gravado, gravado
    assert "maria+da+silva" not in html
    assert "joao souza" not in publico, publico


# B3 ------------------------------------------------------------------ CNPJ alfanumerico


def test_b3_cnpj_alfanumerico_vira_outro_cnpj(base_local):
    """12345678000K19 (DV valido, NAO existe na base) devolve a CONTABIL 12345678000195."""
    outro = "12345678000K19"
    assert cv.is_valid(outro)
    achados = cn({"cnpj": outro})
    r = TestClient(app).get("/api/busca/exportar.csv", params={"selecionados": outro, "colunas": "cnpj"})
    assert achados == [], achados
    assert CONTABIL not in r.text, r.text


def test_b3_cnpj_alfanumerico_existente_nao_e_encontrado(base_alfa):
    assert ALFA in cn({})  # esta na base
    r = TestClient(app).get("/api/busca/exportar.csv", params={"selecionados": ALFA, "colunas": "cnpj"})
    assert {"cnpj": cn({"cnpj": ALFA}), "q": cn({"q": ALFA}), "export_tem": ALFA in r.text} == \
        {"cnpj": [ALFA], "q": [ALFA], "export_tem": True}


# B4 ------------------------------------------------------------------ 500 em entradas do usuario


@pytest.mark.parametrize("qs", [
    "idade_min=99999", "idade_max=9999", "idade_min=nan", "socio_multiempresas=nan",
    "arvore=" + json.dumps({"op": "E", "itens": 5}),
])
def test_b4_entrada_invalida_gera_500(base_local, qs):
    c = TestClient(app, raise_server_exceptions=False)
    api = c.get("/api/busca?" + qs).status_code
    pagina = c.get("/busca?" + qs).status_code
    assert (api, pagina) == (400, 400)


async def test_b4_mcp_erro_generico(base_local):
    srv = mcp_server.configurar(admin=False)
    async with Client(srv) as c:
        r = await c.call_tool("buscar_empresas", {"filtros": {"idade_min": 99999}})
    assert r.is_error and "Filtro inválido" in r.content[0].text, r.content[0].text


# B5 ------------------------------------------------------------------ listas 500


@pytest.mark.parametrize("item", [
    {"cnpj": CONTABIL, "observacao": 5},
    {"cnpj": CONTABIL, "razao_social": {"x": 1}},
])
def test_b5_lista_item_com_tipo_errado_gera_500(base_local, item):
    c = TestClient(app, raise_server_exceptions=False)
    lid = c.post("/api/listas", json={"nome": "L"}).json()["id"]
    r = c.post(f"/api/listas/{lid}/itens", json={"itens": [item]})
    assert r.status_code < 500, r.text


# B6 ------------------------------------------------------------------ busca salva com arvore


async def test_b6_busca_salva_com_arvore_nao_abre_na_tela(base_local):
    srv = mcp_server.configurar(admin=False)
    arvore = {"op": "OU", "itens": [{"campo": "uf", "valor": ["GO"]}, {"campo": "cnae_principal", "valor": ["4711302"]}]}
    async with Client(srv) as c:
        b = json.loads((await c.call_tool("salvar_busca", {"nome": "arv", "filtros": {"arvore": arvore}})).content[0].text)
        r = await c.call_tool("executar_busca_salva", {"busca_id": b["id"]})
        assert json.loads(r.content[0].text)["total"] == 2  # pelo MCP funciona
    pg = TestClient(app).get(f"/busca?salva={b['id']}", follow_redirects=True)
    assert pg.status_code == 200 and "JSON inválido" not in pg.text, pg.status_code


# B7 ------------------------------------------------------------------ numero no formato brasileiro


def test_b7_capital_com_ponto_de_milhar(base_local):
    assert cn({"capital_min": "100.000,00"}) == [MERCADO]
    assert cn({"capital_min": "100.000"}) == [MERCADO]  # hoje vira 100.0 e traz todas


# B8 ------------------------------------------------------------------ negar como texto


def test_b8_arvore_negar_texto_nao(base_local):
    arv = json.dumps({"campo": "uf", "valor": ["TO"], "negar": "nao"})
    assert cn({"arvore": arv}) == sorted([CONTABIL, FILIAL, MERCADO])


# B9 ------------------------------------------------------------------ lista aceita CNPJ invalido


async def test_b9_lista_aceita_cnpj_com_dv_invalido(base_local):
    srv = mcp_server.configurar(admin=False)
    async with Client(srv) as c:
        lst = json.loads((await c.call_tool("criar_lista", {"nome": "P"})).content[0].text)
        r = json.loads((await c.call_tool("adicionar_a_lista", {
            "lista_id": lst["id"], "cnpjs": ["12345678000100", "00000000000000"]})).content[0].text)
    assert r == {"adicionados": 0}  # docstring: "ignora repetidos e invalidos"
