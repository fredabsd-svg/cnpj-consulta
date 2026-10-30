"""Regressao do Gauntlet Loop (rodada 3, seguranca e LGPD): cada teste foi escrito por um
critico independente, reproduziu um defeito real em b5704d8 e hoje guarda a correcao."""

from __future__ import annotations

import json
import os

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import app

CNPJ_CONTABIL = "12345678000195"
CPF_MEI = "12345678900"  # dataset_padrao: razao social "JOSE PEREIRA 12345678900"

# O que o navegador manda num <img src="http://127.0.0.1:8000/..."> de outro site
IMG_DE_OUTRO_SITE = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors",
                     "sec-fetch-dest": "image", "host": "127.0.0.1:8000"}


def _cli(**kw) -> TestClient:
    return TestClient(app, raise_server_exceptions=bool(os.environ.get("GAUNTLET_TRACE")), **kw)


# ---------------------------------------------------------------- S3-1 CPF do MEI no historico de buscas


def test_s3_1_cpf_do_mei_fica_no_historico_de_buscas(base_local):
    """sem_cpf foi aplicado so ao historico de CONSULTAS. Quem digita a razao social
    de um MEI na pagina inicial (/consulta -> /busca?q=...) ou na busca rapida grava
    o CPF do titular em buscas_recentes (filtros e resumo), listado por
    /api/buscas-recentes e na tela /busca, sem login."""
    c = _cli()
    r = c.get("/consulta", params={"cnpj": "JOSE PEREIRA 12345678900"})
    assert r.status_code == 200, r.status_code  # seguiu o redirect para /busca e achou o MEI
    recentes = c.get("/api/buscas-recentes").json()
    assert CPF_MEI not in json.dumps(recentes), recentes
    assert CPF_MEI not in c.get("/busca").text


# ---------------------------------------------------------------- S3-2 contatos em massa pelo MCP/chat sem finalidade


async def test_s3_2_buscar_empresas_do_mcp_entrega_contatos_sem_finalidade(base_local):
    """/api/busca passou a omitir telefones/e-mail sem `finalidade` (e auditar quando
    ha). A ferramenta MCP `buscar_empresas` (tambem usada pelo chat) chama a mesma
    ba.buscar com COLUNAS_TELA: devolve telefone_1/telefone_2/email de ate 100
    empresas por pagina, paginavel sem limite, sem finalidade e sem auditoria."""
    from mcp import Client

    from app import mcp_server
    from app.receita import jobs

    srv = mcp_server.configurar(admin=False)
    async with Client(srv) as c:
        r = await c.call_tool("buscar_empresas", {"filtros": {"uf": ["TO"]}, "por_pagina": 100})
    dados = json.loads(r.content[0].text)
    vazados = [i for i in dados["itens"] if i.get("email") or i.get("telefone_1")]
    auditados = [a for a in jobs.auditoria() if "contato" in a["acao"] or "busca" in a["acao"]]
    assert not vazados or auditados, (vazados[:2], auditados)


# ---------------------------------------------------------------- S3-3 GET de outro site nas paginas HTML


def test_s3_3_img_de_outro_site_grava_historico_pela_pagina_html(base_local):
    """O bloqueio de GET de outro site so cobre /api/, .csv/.xlsx e /exportar.
    /api/companies/{cnpj} e bloqueado, mas a mesma consulta pela pagina
    /empresa/{cnpj} (que grava historico, gasta a cota do limite e, com
    ?externas=1, consulta APIs de terceiros) passa: um <img> num site qualquer
    injeta empresas no historico do usuario."""
    from app.services.history_service import recent_queries

    c = _cli()
    api = c.get(f"/api/companies/{CNPJ_CONTABIL}", headers=IMG_DE_OUTRO_SITE)
    assert api.status_code == 403  # controle: a API esta protegida
    pagina = c.get(f"/empresa/{CNPJ_CONTABIL}", headers=IMG_DE_OUTRO_SITE)
    hist = [h["cnpj"] for h in recent_queries(10)]
    assert pagina.status_code == 403 and not hist, (pagina.status_code, hist)


def test_s3_3b_img_de_outro_site_dispara_pesquisa_paga_na_internet(base_local, monkeypatch):
    """/api/companies/{cnpj}/web-search e bloqueado para outro site, mas
    /empresa/{cnpj}/internet?q=<texto do atacante> faz a mesma pesquisa paga
    (Brave/Tavily com a chave do usuario) a partir de um <img> de outro site."""
    from app.config import get_settings

    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "chave-paga-do-usuario")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as m:
        brave = m.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(200, json={"web": {"results": []}}))
        c = _cli()
        api = c.get(f"/api/companies/{CNPJ_CONTABIL}/web-search", params={"q": "texto do atacante"},
                    headers=IMG_DE_OUTRO_SITE)
        assert api.status_code == 403  # controle
        c.get(f"/empresa/{CNPJ_CONTABIL}/internet", params={"q": "texto do atacante"}, headers=IMG_DE_OUTRO_SITE)
    enviados = [str(call.request.url.params.get("q")) for call in brave.calls]
    assert not enviados, enviados


# ---------------------------------------------------------------- S3-4 exportacao por GET "same-site"


def test_s3_4_exportacao_com_contatos_disparada_por_outra_porta_do_localhost(base_local):
    """O bloqueio compara sec-fetch-site == "cross-site". Para o navegador,
    http://localhost:3000 e http://localhost:8000 sao o MESMO site (porta nao conta):
    qualquer outra pagina servida em localhost (dev server, outro app local) manda
    "same-site" e um <img> dispara a exportacao com contatos + finalidade escolhida
    pelo atacante, gravada na auditoria. Os POST nao tem esse furo (conferem Origin)."""
    from app.receita import jobs

    # controle: um POST vindo de localhost:3000 e bloqueado
    p = _cli().post("/historico/limpar", headers={"host": "localhost:8000", "sec-fetch-site": "same-site",
                                                  "origin": "http://localhost:3000"})
    assert p.status_code == 403
    h = {"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image",
         "host": "localhost:8000", "referer": "http://localhost:3000/"}
    r = _cli().get("/api/busca/exportar.csv",
                   params={"colunas": ["cnpj", "email", "telefone_1"], "finalidade": "consentimento do titular"},
                   headers=h)
    registros = [i for i in jobs.auditoria() if i["acao"] == "busca.exportar"]
    assert r.status_code == 403 and not registros, (r.status_code, registros)


# ---------------------------------------------------------------- S3-5 ip_de troca IP remoto por 127.0.0.1


def test_s3_5_cliente_remoto_com_x_forwarded_for_vira_127001_na_auditoria(db_initialized):
    """ip_de devolve a constante "127.0.0.1" sempre que ha X-Forwarded-For e
    CONFIAR_PROXY=false, mesmo quando o par TCP NAO e loopback (app em 0.0.0.0, modo
    laboratorio). Um cliente remoto so precisa mandar `X-Forwarded-For: x` para que
    suas tentativas de token apareçam na auditoria como se viessem da propria maquina
    (e para cair no mesmo balde do limite que o administrador local)."""
    from app.receita import jobs

    c = TestClient(app, client=("203.0.113.9", 40000), raise_server_exceptions=False)
    c.get("/api/admin/auditoria", headers={"Authorization": "Bearer chute", "X-Forwarded-For": "qualquer"})
    ips = [a["ip"] for a in jobs.auditoria() if a["acao"] == "admin.login"]
    assert ips == ["203.0.113.9"], ips


# ---------------------------------------------------------------- S3-6 historico de buscas nao pode ser apagado


def test_s3_6_historico_de_buscas_nao_tem_como_ser_apagado(base_local):
    """listas.limpar_recentes existe mas nenhuma rota, comando ou ferramenta a chama.
    "Apagar histórico" (/historico/limpar, DELETE /api/history) so apaga as consultas
    de CNPJ: os termos das buscas (inclusive o CPF do S3-1) continuam em
    /api/buscas-recentes e na tela /busca, sem meio de exclusao pelo titular/usuario."""
    c = _cli()
    c.get("/busca", params={"q": "JOSE PEREIRA 12345678900"})
    c.get("/busca", params={"uf": "TO", "email_dominio": "gmail.com"})
    assert c.get("/api/buscas-recentes").json()  # controle: as buscas foram registradas
    c.post("/historico/limpar")
    c.delete("/api/history")
    c.delete("/api/buscas-recentes")
    sobrou = c.get("/api/buscas-recentes").json()
    assert not sobrou, [b["resumo"] for b in sobrou]
