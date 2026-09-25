"""Testes das paginas HTML, seguranca e fluxo de historico/favoritos."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app

CNPJ = "19131243000197"


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


def _mock_sources(brasilapi_payload, receitaws_payload):
    respx.get(f"https://minhareceita.org/{CNPJ}").mock(return_value=httpx.Response(200, json=brasilapi_payload))
    respx.get(f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}").mock(
        return_value=httpx.Response(200, json=brasilapi_payload)
    )
    respx.get(f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}").mock(
        return_value=httpx.Response(200, json=receitaws_payload)
    )


class TestPages:
    @pytest.mark.parametrize("path", ["/", "/socios", "/historico", "/privacidade"])
    def test_pages_render(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_security_headers(self, client):
        r = client.get("/")
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"

    def test_no_external_scripts(self, client):
        assert "cdnjs" not in client.get("/").text


class TestConsultaFlow:
    def test_valid_cnpj_redirects(self, client):
        r = client.get("/consulta", params={"cnpj": "19.131.243/0001-97"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/empresa/{CNPJ}"

    def test_invalid_cnpj_shows_inline_error(self, client):
        r = client.get("/consulta", params={"cnpj": "11.111.111/1111-11"})
        assert r.status_code == 400
        assert "CNPJ inválido" in r.text
        assert 'aria-invalid="true"' in r.text

    def test_input_is_escaped(self, client):
        """Regressao: o CNPJ era refletido sem escape (XSS)."""
        payload = "<img src=x onerror=alert(1)>"
        r = client.get("/consulta", params={"cnpj": payload})
        assert payload[:18] not in r.text
        assert "&lt;img" in r.text

    @respx.mock
    def test_company_page(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        r = client.get(f"/empresa/{CNPJ}")
        assert r.status_code == 200
        assert "OPEN KNOWLEDGE BRASIL" in r.text
        assert "03/10/2013" in r.text  # data no padrao brasileiro
        assert "9430-8/00" in r.text  # CNAE formatado
        assert "As fontes divergem" not in r.text

    @respx.mock
    def test_company_not_found_page(self, client):
        for url in (
            f"https://minhareceita.org/{CNPJ}",
            f"https://brasilapi.com.br/api/cnpj/v1/{CNPJ}",
            f"https://www.receitaws.com.br/v1/cnpj/{CNPJ}",
        ):
            respx.get(url).mock(return_value=httpx.Response(404, json={}))
        r = client.get(f"/empresa/{CNPJ}")
        assert r.status_code == 404
        assert "Nenhuma fonte retornou dados" in r.text


class TestCsrf:
    def test_cross_site_post_blocked(self, client):
        r = client.post(f"/api/favorites/{CNPJ}", headers={"Origin": "https://malicioso.example"})
        assert r.status_code == 403

    def test_cross_site_delete_blocked(self, client):
        r = client.delete("/api/history", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403

    def test_same_origin_allowed(self, client):
        r = client.post(f"/api/favorites/{CNPJ}", headers={"Origin": "http://testserver"})
        assert r.status_code == 200


class TestHistoryAndFavorites:
    @respx.mock
    def test_history_recorded_even_on_cache_hit(self, client, brasilapi_payload, receitaws_payload):
        _mock_sources(brasilapi_payload, receitaws_payload)
        client.get(f"/api/companies/{CNPJ}")
        client.get(f"/api/companies/{CNPJ}")  # cache
        assert client.get("/api/history").json()["total"] == 2

    def test_clear_history(self, client):
        assert client.delete("/api/history").status_code == 200

    def test_favorite_roundtrip(self, client):
        assert client.post(f"/api/favorites/{CNPJ}", params={"label": "OKB"}).status_code == 200
        favs = client.get("/api/favorites").json()["favorites"]
        assert favs[0]["cnpj"] == CNPJ
        assert "OKB" in client.get("/historico").text
        assert client.delete(f"/api/favorites/{CNPJ}").json()["removed"] is True

    def test_favorite_invalid_cnpj(self, client):
        assert client.post("/api/favorites/123").status_code == 400
