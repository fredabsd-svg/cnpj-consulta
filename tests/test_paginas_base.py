"""Paginas da base local: ficha, busca avancada, listas, socio e painel admin."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

CNPJ = "12345678000195"


def test_ficha_completa_na_pagina_da_empresa(base_local):
    r = TestClient(app).get(f"/empresa/{CNPJ}")
    assert r.status_code == 200, r.text[:2000]
    html = r.text
    for aba in ("Resumo", "Endereço", "Contatos", "CNAEs", "Sócios", "Histórico"):
        assert aba in html
    assert "CONTABIL TESTE LTDA" in html
    assert "***.123.456-**" in html  # CPF mascarado como publicado
    assert "Base local RFB" in html
    assert "(63) 9234-5678" in html and "celular" in html and "sem o nono dígito" in html


def test_busca_avancada_pagina(base_local):
    c = TestClient(app)
    r = c.get("/busca?uf=TO&situacao=2&atividade=contabilidade")
    assert r.status_code == 200, r.text[:3000]
    assert "CONTABIL TESTE LTDA" in r.text
    assert "MERCADO EXEMPLO" not in r.text
    assert c.get("/busca").status_code == 200


def test_socio_e_listas(base_local):
    c = TestClient(app)
    r = c.get("/busca?socio_nome=maria")
    assert r.status_code == 200
    from app.services.ficha import ficha_local

    chave = next(s["chave"] for s in ficha_local(CNPJ)["socios"] if s["nome"] == "MARIA DA SILVA")
    r = c.get(f"/socio/{chave}")
    assert r.status_code == 200 and "MERCADO EXEMPLO LTDA" in r.text
    assert c.get("/listas").status_code == 200


def test_painel_admin(base_local, admin_headers):
    c = TestClient(app)
    r = c.get("/base", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/entrar")
    r = c.get("/base", headers=admin_headers)
    assert r.status_code == 200, r.text[:3000]
    assert "Versão ativa" in r.text and "2026-10" in r.text
