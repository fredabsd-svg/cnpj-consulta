"""Layout: filtros rapidos, atalhos de prospeccao, contato rapido na ficha e
relatorio de diligencia."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import app
from app.web import pages_base

CNPJ = "12345678000195"


def test_atalho_vira_filtros_da_busca(base_local):
    c = TestClient(app, follow_redirects=False)
    r = c.get("/busca?atalho=novas&uf=TO")
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["uf"] == ["TO"] and q["situacao"] == ["2"] and q["ordem"] == ["abertura"]
    assert q["abertura_de"] == [(date.today() - timedelta(days=30)).isoformat()]
    q = parse_qs(urlsplit(c.get("/busca?atalho=inaptas").headers["location"]).query)
    assert q == {"situacao": ["4"]}  # sem estado: Brasil inteiro
    assert c.get("/busca?atalho=nao-existe").status_code == 200  # atalho desconhecido e ignorado
    for chave in pages_base.ATALHOS:  # todo atalho gera filtros validos
        destino = c.get(f"/busca?atalho={chave}&uf=TO").headers["location"]
        assert TestClient(app).get(destino).status_code == 200, chave


def test_filtros_rapidos_ligam_e_desligam(base_local):
    rapidos = {r["rotulo"]: r for r in pages_base._filtros_rapidos({"uf": ["TO"], "situacao": ["4"]})}
    assert not rapidos["Só ativas"]["ativo"]
    q = parse_qs(rapidos["Só ativas"]["href"])
    assert q["situacao"] == ["4", "2"] and q["uf"] == ["TO"]  # soma ao que ja estava marcado
    assert parse_qs(rapidos["Com e-mail"]["href"])["tem_email"] == ["sim"]
    rapidos = {r["rotulo"]: r for r in pages_base._filtros_rapidos({"uf": ["TO"], "situacao": ["2"], "pagina": ["3"]})}
    assert rapidos["Só ativas"]["ativo"]
    assert parse_qs(rapidos["Só ativas"]["href"]) == {"uf": ["TO"]}  # desliga e volta para a pagina 1
    rapidos = {r["rotulo"]: r for r in pages_base._filtros_rapidos({"abertura_de": ["2020-01-01"]})}
    assert rapidos["Abertas nos últimos 12 meses"]["ativo"]
    assert "abertura_de" not in rapidos["Abertas nos últimos 12 meses"]["href"]


def test_pagina_da_busca_mostra_filtros_rapidos_exportar_e_atalhos(base_local):
    c = TestClient(app)
    vazia = c.get("/busca").text
    assert "Comece por um atalho" in vazia and 'name="atalho" value="novas"' in vazia
    assert "Filtros rápidos" in vazia
    pagina = c.get("/busca?uf=TO&situacao=2").text
    assert 'class="chip chip-rapido ativo"' in pagina and 'aria-pressed="true"' in pagina
    assert 'href="#exportar"' in pagina and 'id="exportar"' in pagina
    assert "data-ver-duplicidades" in pagina  # contatos repetidos: so quando pedido
    assert pagina.index("<summary>Localização</summary>") < pagina.index("<summary>Identificação</summary>")
    assert "Comece por um atalho" in c.get("/").text


def test_duplicidades_nao_sao_buscadas_sozinhas():
    js = (Path(pages_base.__file__).parents[1] / "static" / "js" / "base.js").read_text(encoding="utf-8")
    trecho = js[js.index("async function initContadores"):js.index("function initLembrarUf")]
    assert "/api/busca/duplicidades" not in trecho  # so no clique do botao
    assert "/api/busca/duplicidades" in js[js.index("function initDuplicidades"):]


def test_ficha_tem_contato_rapido(base_local):
    pagina = TestClient(app).get(f"/empresa/{CNPJ}").text
    assert "Contato rápido" in pagina
    assert 'href="tel:+556332151234"' in pagina and 'href="mailto:contato@contafacil.com.br"' in pagina
    # WhatsApp so para celular com nono digito: o fixo e o celular publicado com 8 digitos
    # ficam sem o botao (o app nao completa numero)
    assert 'href="tel:+556392345678"' in pagina and "wa.me/" not in pagina
    assert 'class="kv kv-duplo"' in pagina


def test_relatorio_tem_indicadores_resumo_e_checklist(base_local):
    r = TestClient(app).get(f"/empresa/{CNPJ}/relatorio", params={"escritorio": "Escritório Modelo"})
    assert r.status_code == 200
    corpo = r.text
    for trecho in ("Escritório Modelo", "Tempo de atividade", "Parecer", "Resumo executivo",
                   "Checklist de diligência", "Dados cadastrais", "Quadro societário", "Fontes consultadas",
                   "é a matriz de Sociedade Empresária Limitada", "Capital social declarado de R$ 15.000,00",
                   "2 sócios no quadro societário", "1 filial listada", "data-personalizar"):
        assert trecho in corpo, trecho
    assert "r$ 15.000,00" not in corpo and "SEM MOTIVO" not in corpo
    assert "style=" not in corpo  # CSP: nada de estilo inline
