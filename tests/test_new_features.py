"""Testes das funcoes novas: pesquisa na internet, lote, sancoes e status."""

from __future__ import annotations

import csv
import io
from datetime import date
from urllib.parse import unquote_plus

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.formatting import fontes_no_texto, idade
from app.main import app
from app.schemas.company import Address, CompanyUnified, PartnerPublic
from app.services import sanctions, web_research
from app.services.batch import parse_cnpj_list
from app.services.diligence import build_diligence_checklist
from app.services.sanctions import SanctionsCheck

CNPJ = "19131243000197"
CNPJ_ALFA = "12ABC34501DE35"


@pytest.fixture(autouse=True)
def _limpa_caches():
    web_research.clear_web_cache()
    sanctions.clear_sanctions_cache()
    yield
    web_research.clear_web_cache()
    sanctions.clear_sanctions_cache()


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


def _company(**kw) -> CompanyUnified:
    data = dict(
        cnpj=CNPJ,
        cnpj_formatado="19.131.243/0001-97",
        razao_social="OPEN KNOWLEDGE BRASIL",
        nome_fantasia="REDE PELO CONHECIMENTO LIVRE",
        situacao_cadastral="ATIVA",
        email="contato@okbr.org.br",
        endereco=Address(logradouro="AV PAULISTA", numero="37", municipio="SAO PAULO", uf="SP", cep="01311902"),
        socios=[PartnerPublic(nome="Maria Exemplo Da Silva", fonte="minha_receita")],
    )
    data.update(kw)
    return CompanyUnified(**data)


def _mock_sources(payload, receitaws=None):
    respx.get(f"https://minhareceita.org/{CNPJ}").mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}").mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}").mock(
        return_value=httpx.Response(200, json=receitaws) if receitaws else httpx.Response(404)
    )


# ---------------------------------------------------------------------------
# Atalhos de pesquisa
# ---------------------------------------------------------------------------


class TestResearchLinks:
    def test_grupos_e_links_https(self):
        groups = web_research.build_research_links(_company())
        ids = [g.id for g in groups]
        assert ids == ["buscadores", "reputacao", "sancoes", "oficiais", "presenca"]
        for g in groups:
            assert g.links, g.id
            for link in g.links:
                assert link.url.startswith("https://"), link.url

    def test_nunca_inclui_nome_de_socio(self):
        """LGPD: nenhum link pode levar o nome do socio a terceiros."""
        groups = web_research.build_research_links(_company())
        for g in groups:
            for link in g.links:
                texto = unquote_plus(link.url).upper()
                assert "MARIA" not in texto and "EXEMPLO DA SILVA" not in texto

    def test_razao_social_entre_aspas_e_codificada(self):
        google = web_research.build_research_links(_company())[0].links[0]
        assert "q=%22OPEN+KNOWLEDGE+BRASIL%22" in google.url

    def test_comprovante_ja_vem_com_cnpj_e_sem_copiar(self):
        oficiais = next(g for g in web_research.build_research_links(_company()) if g.id == "oficiais")
        comprovante = oficiais.links[0]
        assert comprovante.url.endswith(f"?cnpj={CNPJ}")
        assert comprovante.manual is False
        assert any(link.manual for link in oficiais.links)  # CND, CRF, CNDT pedem digitacao

    def test_cnpj_alfanumerico_preservado(self):
        c = _company(cnpj=CNPJ_ALFA, cnpj_formatado="12.ABC.345/01DE-35")
        sancoes = next(g for g in web_research.build_research_links(c) if g.id == "sancoes")
        assert all(CNPJ_ALFA in link.url for link in sancoes.links)

    def test_dominio_corporativo_vira_site_e_whois(self):
        presenca = next(g for g in web_research.build_research_links(_company()) if g.id == "presenca")
        urls = [link.url for link in presenca.links]
        assert "https://okbr.org.br" in urls
        assert any("registro.br" in u for u in urls)
        assert any("google.com/maps" in u for u in urls)

    @pytest.mark.parametrize(
        ("email", "esperado"),
        [
            ("fulano@gmail.com", None),
            ("contato@empresa.com.br", "empresa.com.br"),
            ("x@EMPRESA.COM", "empresa.com"),
            ("invalido", None),
            (None, None),
        ],
    )
    def test_corporate_domain(self, email, esperado):
        assert web_research.corporate_domain(email) == esperado

    def test_default_query_usa_cidade(self):
        assert web_research.default_query(_company()) == '"OPEN KNOWLEDGE BRASIL" SAO PAULO SP'


# ---------------------------------------------------------------------------
# Resultados na tela (provedores)
# ---------------------------------------------------------------------------


class TestWebSearch:
    async def test_desligado_sem_provedor(self):
        r = await web_research.search_web("x")
        assert r.habilitado is False and r.resultados == []

    @respx.mock
    async def test_brave_parse_filtra_url_perigosa_e_limpa_html(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "k")
        get_settings.cache_clear()
        route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {"title": "OKBR", "url": "https://www.ok.org.br/sobre", "description": "A <strong>OKBR</strong> &amp; dados"},
                            {"title": "XSS", "url": "javascript:alert(1)", "description": "x"},
                        ]
                    }
                },
            )
        )
        r = await web_research.search_web('"OPEN KNOWLEDGE BRASIL"')
        assert route.called
        assert route.calls[0].request.headers["X-Subscription-Token"] == "k"
        assert [x.dominio for x in r.resultados] == ["ok.org.br"]
        assert r.resultados[0].trecho == "A OKBR & dados"
        assert r.atribuicao == "Powered by Brave Search"
        # segunda chamada vem do cache, sem nova requisicao
        again = await web_research.search_web('"OPEN KNOWLEDGE BRASIL"')
        assert again.do_cache and route.call_count == 1

    @respx.mock
    async def test_tavily_post_com_bearer(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-1")
        get_settings.cache_clear()
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"results": [{"title": "T", "url": "https://a.com", "content": "c"}]})
        )
        r = await web_research.search_web("empresa")
        assert route.calls[0].request.headers["Authorization"] == "Bearer tvly-1"
        assert r.resultados[0].url == "https://a.com"

    @respx.mock
    async def test_searxng_json(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")
        monkeypatch.setenv("WEB_SEARCH_URL", "http://127.0.0.1:8888/")
        get_settings.cache_clear()
        route = respx.get("http://127.0.0.1:8888/search").mock(
            return_value=httpx.Response(200, json={"results": [{"title": "S", "url": "https://s.com", "content": "z"}]})
        )
        r = await web_research.search_web("empresa")
        assert route.calls[0].request.url.params["format"] == "json"
        assert r.provedor == "SearXNG" and r.resultados[0].titulo == "S"

    @respx.mock
    async def test_erro_de_chave_vira_mensagem(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "ruim")
        get_settings.cache_clear()
        respx.get("https://api.search.brave.com/res/v1/web/search").mock(return_value=httpx.Response(401))
        r = await web_research.search_web("x")
        assert r.resultados == [] and "WEB_SEARCH_API_KEY" in r.erro

    @respx.mock
    async def test_searxng_sem_json_explica(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")
        monkeypatch.setenv("WEB_SEARCH_URL", "http://127.0.0.1:8888")
        get_settings.cache_clear()
        respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(403))
        r = await web_research.search_web("x")
        assert "json" in r.erro


# ---------------------------------------------------------------------------
# Sancoes (Portal da Transparencia)
# ---------------------------------------------------------------------------


class TestSanctions:
    async def test_desligado_devolve_none(self):
        assert await sanctions.check_sanctions(CNPJ) is None

    @respx.mock
    async def test_sancionada(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave")
        get_settings.cache_clear()
        route = respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(200, json={"sancionadoCEIS": True, "sancionadoCNEP": False})
        )
        r = await sanctions.check_sanctions(CNPJ)
        assert route.calls[0].request.headers["chave-api-dados"] == "chave"
        assert route.calls[0].request.url.params["cnpj"] == CNPJ
        assert r.consultado and r.sancionada and r.cadastros[0].startswith("CEIS")

    @respx.mock
    async def test_chave_recusada(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "ruim")
        get_settings.cache_clear()
        respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(401)
        )
        r = await sanctions.check_sanctions(CNPJ)
        assert r.consultado is False and "recusada" in r.erro

    def test_itens_da_diligencia(self):
        def item(s):
            return next(i for i in build_diligence_checklist(_company(), s) if i.id == "sancoes")

        assert item(None).status == "info"
        assert item(SanctionsCheck(True)).status == "ok"
        assert item(SanctionsCheck(True, ["CEIS (inidôneas e suspensas)"])).status == "falha"
        assert item(SanctionsCheck(False, erro="fora do ar")).status == "alerta"


# ---------------------------------------------------------------------------
# Lote
# ---------------------------------------------------------------------------


class TestBatchParse:
    def test_separadores_mascara_duplicados_e_invalidos(self):
        p = parse_cnpj_list("19.131.243/0001-97\n12abc34501de35; 19131243000197, 123 xpto", 30)
        assert p.validos == [CNPJ, CNPJ_ALFA]
        assert p.invalidos == ["123", "xpto"]
        assert p.excedentes == 0

    def test_limite_maximo(self):
        p = parse_cnpj_list(f"{CNPJ} {CNPJ_ALFA}", 1)
        assert p.validos == [CNPJ] and p.excedentes == 1


class TestBatchApi:
    @respx.mock
    def test_json_e_csv(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get("/api/batch", params={"cnpjs": f"{CNPJ}, 11.111.111/1111-11"})
        assert r.status_code == 200
        body = r.json()
        assert body["validos"] == 1 and body["invalidos"] == ["11.111.111/1111-11"]
        row = body["resultados"][0]
        assert row["status"] == "ok" and row["razao_social"] == "OPEN KNOWLEDGE BRASIL"
        assert row["veredicto"] in {"regular", "ressalvas", "critico"}

        csv_resp = client.get("/api/batch/export.csv", params={"cnpjs": CNPJ})
        assert csv_resp.status_code == 200
        assert "attachment" in csv_resp.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(csv_resp.content.decode("utf-8-sig")), delimiter=";"))
        assert rows[0][:2] == ["CNPJ", "Razão social"]
        assert rows[1][0] == "19.131.243/0001-97"  # formatado: Excel nao come os zeros
        assert rows[1][1] == "OPEN KNOWLEDGE BRASIL"
        assert "Não" in rows[1]  # booleanos em portugues

    def test_sem_cnpj_valido_400(self, client):
        assert client.get("/api/batch", params={"cnpjs": "abc"}).status_code == 400

    @respx.mock
    def test_limite_de_entrada_so_conta_cache_miss(self, client, monkeypatch, brasilapi_payload):
        monkeypatch.setenv("INBOUND_RATE_LIMIT_PER_MINUTE", "1")
        get_settings.cache_clear()
        from app.core.inbound_limit import reset_inbound_limiter

        reset_inbound_limiter()
        _mock_sources(brasilapi_payload)
        body = client.get("/api/batch", params={"cnpjs": f"{CNPJ} {CNPJ_ALFA}"}).json()
        status = {r["cnpj"]: r["status"] for r in body["resultados"]}
        assert status[CNPJ] == "ok"
        assert status[CNPJ_ALFA] == "limite"
        # o primeiro agora esta no cache: nao gasta limite
        again = client.get("/api/batch", params={"cnpjs": CNPJ}).json()
        assert again["resultados"][0]["status"] == "ok"
        assert again["resultados"][0]["do_cache"] is True


# ---------------------------------------------------------------------------
# Paginas
# ---------------------------------------------------------------------------


class TestNewPages:
    @pytest.mark.parametrize("path", ["/lote", "/fontes"])
    def test_render(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert "Novo" in r.text  # selo no menu

    @respx.mock
    def test_lote_com_resultado(self, client, brasilapi_payload):
        _mock_sources(brasilapi_payload)
        r = client.get("/lote", params={"cnpjs": f"{CNPJ}\nxpto"})
        assert r.status_code == 200
        assert "OPEN KNOWLEDGE BRASIL" in r.text
        assert "xpto" in r.text  # invalido listado
        assert "/api/batch/export.csv?cnpjs=" in r.text

    def test_lote_so_invalidos(self, client):
        r = client.get("/lote", params={"cnpjs": "abc"})
        assert r.status_code == 400 and "Nenhum CNPJ válido" in r.text

    @respx.mock
    def test_empresa_tem_aba_internet_e_checklist(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get(f"/empresa/{CNPJ}")
        assert r.status_code == 200
        assert 'id="tab-internet"' in r.text
        assert "jusbrasil.com.br/busca" in r.text
        assert "Sanções federais" in r.text
        assert "WEB_SEARCH_PROVIDER" in r.text  # instrucao de como ligar
        assert "MARIA" not in r.text.split('id="internet"')[1].split("</section>")[0].upper()

    @respx.mock
    def test_pagina_internet_com_provedor(self, client, monkeypatch, brasilapi_payload):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")
        monkeypatch.setenv("WEB_SEARCH_URL", "http://127.0.0.1:8888")
        get_settings.cache_clear()
        _mock_sources(brasilapi_payload)
        respx.get("http://127.0.0.1:8888/search").mock(
            return_value=httpx.Response(200, json={"results": [{"title": "Notícia OKBR", "url": "https://n.com/1", "content": "<b>x</b>"}]})
        )
        r = client.get(f"/empresa/{CNPJ}/internet", params={"q": "okbr"})
        assert r.status_code == 200
        assert "Notícia OKBR" in r.text and "https://n.com/1" in r.text
        assert "<b>x</b>" not in r.text

    @respx.mock
    def test_api_research_e_web_search(self, client, brasilapi_payload):
        _mock_sources(brasilapi_payload)
        r = client.get(f"/api/companies/{CNPJ}/research")
        assert r.status_code == 200
        assert r.json()["busca_na_tela"]["habilitada"] is False
        assert {g["id"] for g in r.json()["grupos"]} >= {"buscadores", "oficiais"}
        w = client.get(f"/api/companies/{CNPJ}/web-search").json()
        assert w["habilitado"] is False and w["consulta"].startswith('"OPEN KNOWLEDGE BRASIL"')

    def test_api_sanctions_desligada_503(self, client):
        assert client.get(f"/api/companies/{CNPJ}/sanctions").status_code == 503

    @respx.mock
    def test_inicio_mostra_metricas(self, client, brasilapi_payload):
        _mock_sources(brasilapi_payload)
        client.get(f"/empresa/{CNPJ}")
        r = client.get("/")
        assert "Consultas hoje" in r.text and "Empresas consultadas" in r.text
        assert 'data-atalho-busca' in r.text


# ---------------------------------------------------------------------------
# Filtros de formatacao
# ---------------------------------------------------------------------------


class TestFilters:
    def test_fontes_no_texto(self):
        assert fontes_no_texto("receitaws: 1; minha_receita: 2; receita_local: 3") == (
            "ReceitaWS: 1; Minha Receita: 2; Receita Federal (base local): 3"
        )

    @pytest.mark.parametrize(
        ("inicio", "esperado"),
        [
            (date(2013, 10, 3), "12 anos"),
            (date(2025, 6, 1), "1 ano e 3 meses"),
            (date(2026, 8, 25), "1 mês"),
            (date(2026, 9, 20), "menos de 1 mês"),
            (date(2027, 1, 1), ""),
        ],
    )
    def test_idade(self, inicio, esperado):
        assert idade(inicio, hoje=date(2026, 9, 25)) == esperado


class TestBatchCli:
    @respx.mock
    def test_lote_grava_csv(self, tmp_path, db_initialized, brasilapi_payload):
        from typer.testing import CliRunner

        from app.cli import app as cli_app

        _mock_sources(brasilapi_payload)
        lista = tmp_path / "lista.txt"
        lista.write_text(f"{CNPJ}\nxpto\n", encoding="utf-8")
        saida = tmp_path / "saida.csv"
        result = CliRunner().invoke(cli_app, ["lote", str(lista), "--csv", str(saida)])
        assert result.exit_code == 0, result.output
        assert "1 consultados" in result.output
        linhas = saida.read_bytes().decode("utf-8-sig").splitlines()
        assert linhas[1].startswith("19.131.243/0001-97;OPEN KNOWLEDGE BRASIL")

    def test_lote_sem_cnpj_valido(self, tmp_path):
        from typer.testing import CliRunner

        from app.cli import app as cli_app

        lista = tmp_path / "vazia.txt"
        lista.write_text("abc", encoding="utf-8")
        assert CliRunner().invoke(cli_app, ["lote", str(lista)]).exit_code == 2


class TestSanctionsFailureCache:
    @respx.mock
    async def test_falha_fica_em_cache_curto(self, monkeypatch):
        """Com o Portal fora do ar, a pagina nao espera o timeout a cada abertura."""
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave")
        get_settings.cache_clear()
        route = respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(503)
        )
        first = await sanctions.check_sanctions(CNPJ)
        second = await sanctions.check_sanctions(CNPJ)
        assert first.consultado is False and second is first
        assert route.call_count == 1



# ---------------------------------------------------------------------------
# Achados da revisao independente
# ---------------------------------------------------------------------------


class TestReviewFindings:
    @respx.mock
    def test_lote_fonte_limitada_nao_vira_nao_encontrada(self, client):
        for url in (
            f"https://minhareceita.org/{CNPJ}",
            f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}",
            f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}",
        ):
            respx.get(url).mock(return_value=httpx.Response(429))
        row = client.get("/api/batch", params={"cnpjs": CNPJ}).json()["resultados"][0]
        assert row["status"] == "limite"

    @respx.mock
    def test_lote_404_em_todas_e_nao_encontrada(self, client):
        for url in (
            f"https://minhareceita.org/{CNPJ}",
            f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}",
            f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}",
        ):
            respx.get(url).mock(return_value=httpx.Response(404))
        row = client.get("/api/batch", params={"cnpjs": CNPJ}).json()["resultados"][0]
        assert row["status"] == "nao_encontrado"

    @respx.mock
    def test_veredicto_do_lote_considera_sancoes(self, client, monkeypatch, brasilapi_payload):
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave")
        get_settings.cache_clear()
        _mock_sources(brasilapi_payload)
        respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(200, json={"sancionadoCEIS": True})
        )
        row = client.get("/api/batch", params={"cnpjs": CNPJ}).json()["resultados"][0]
        assert row["veredicto"] == "critico"
        assert row["sancoes"].startswith("CEIS")

    @respx.mock
    async def test_sancoes_resposta_sem_indicadores_nao_e_limpa(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave")
        get_settings.cache_clear()
        respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(200, json={"mensagem": "CNPJ nao encontrado"})
        )
        r = await sanctions.check_sanctions(CNPJ)
        assert r.consultado is False

    @respx.mock
    async def test_atualizar_ignora_cache_de_sancoes(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave")
        get_settings.cache_clear()
        route = respx.get("https://api.portaldatransparencia.gov.br/api-de-dados/pessoa-juridica").mock(
            return_value=httpx.Response(200, json={"sancionadoCEIS": False})
        )
        await sanctions.check_sanctions(CNPJ)
        await sanctions.check_sanctions(CNPJ, force_refresh=True)
        assert route.call_count == 2

    def test_dominio_exibido_ignora_usuario_na_url(self):
        r = web_research._safe_result("x", "https://www.google.com@evil.example/p", "")
        assert r.dominio == "evil.example"

    @respx.mock
    def test_pagina_internet_nao_pesquisa_empresa_inexistente(self, client, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")
        monkeypatch.setenv("WEB_SEARCH_URL", "http://127.0.0.1:8888")
        get_settings.cache_clear()
        for url in (
            f"https://minhareceita.org/{CNPJ}",
            f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}",
            f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}",
        ):
            respx.get(url).mock(return_value=httpx.Response(404))
        busca = respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(200, json={}))
        r = client.get(f"/empresa/{CNPJ}/internet")
        assert r.status_code == 404
        assert not busca.called

    @respx.mock
    def test_cli_lote_arquivo_ansi_e_markup(self, tmp_path, db_initialized, brasilapi_payload):
        from typer.testing import CliRunner

        from app.cli import app as cli_app

        _mock_sources(brasilapi_payload)
        lista = tmp_path / "ansi.csv"
        lista.write_bytes(f"CNPJ;Razão\n{CNPJ};Cliente [/b]\n".encode("cp1252"))
        saida = tmp_path / "saida.csv"
        result = CliRunner().invoke(cli_app, ["lote", str(lista), "--csv", str(saida)])
        assert result.exit_code == 0, result.output
        assert saida.exists()


# ---------------------------------------------------------------------------
# Cartao CNPJ oficial dentro do app
# ---------------------------------------------------------------------------


class TestCartaoCnpj:
    @respx.mock
    def test_pagina_embute_site_oficial_com_cnpj(self, client, brasilapi_payload):
        _mock_sources(brasilapi_payload)
        r = client.get(f"/empresa/{CNPJ}/cartao-cnpj")
        assert r.status_code == 200
        oficial = (
            "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/"
            f"Cnpjreva_Solicitacao.asp?cnpj={CNPJ}"
        )
        assert f'<iframe class="official-frame" src="{oficial}"' in r.text
        assert "sandbox=" in r.text and "allow-top-navigation" not in r.text
        assert "OPEN KNOWLEDGE BRASIL" in r.text
        csp = r.headers["content-security-policy"]
        assert "frame-src https://solucoes.receita.fazenda.gov.br" in csp
        assert "frame-ancestors 'none'" in csp  # o app continua sem poder ser enquadrado

    @respx.mock
    def test_funciona_mesmo_sem_fontes(self, client):
        """Quem emite e a Receita: a pagina abre mesmo com as APIs fora do ar."""
        for url in (
            f"https://minhareceita.org/{CNPJ}",
            f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}",
            f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}",
        ):
            respx.get(url).mock(return_value=httpx.Response(503))
        r = client.get(f"/empresa/{CNPJ}/cartao-cnpj")
        assert r.status_code == 200
        assert "official-frame" in r.text

    def test_cnpj_alfanumerico_na_url_oficial(self):
        from app.services.official_docs import comprovante_url

        assert comprovante_url(CNPJ_ALFA).endswith("?cnpj=12ABC34501DE35")

    def test_cnpj_invalido(self, client):
        assert client.get("/empresa/123/cartao-cnpj").status_code == 400

    @respx.mock
    def test_empresa_tem_botoes_cartao_e_ie(self, client, brasilapi_payload):
        _mock_sources(brasilapi_payload)
        r = client.get(f"/empresa/{CNPJ}")
        assert f"/empresa/{CNPJ}/cartao-cnpj" in r.text
        assert f"/empresa/{CNPJ}/inscricao-estadual" in r.text
