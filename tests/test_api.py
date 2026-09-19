"""Testes da API REST."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db_initialized):
    return TestClient(app)


class TestHealth:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body


class TestProvidersStatus:
    def test_list_providers(self, client):
        r = client.get("/api/providers/status")
        assert r.status_code == 200
        body = r.json()
        assert "providers" in body
        names = [p["name"] for p in body["providers"]]
        assert "minha_receita" in names
        assert "brasilapi" in names


class TestCompanies:
    @respx.mock
    def test_company_invalid_cnpj(self, client):
        r = client.get("/api/companies/123")
        assert r.status_code == 400

    @respx.mock
    def test_company_success(self, client, brasilapi_payload):
        # Mock ambos provedores
        respx.get("https://minhareceita.org/19131243000197").mock(
            return_value=httpx.Response(200, json=brasilapi_payload)
        )
        respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            return_value=httpx.Response(200, json=brasilapi_payload)
        )
        respx.get("https://www.receitaws.com.br/v1/cnpj/19131243000197").mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )

        r = client.get("/api/companies/19131243000197")
        assert r.status_code == 200
        body = r.json()
        assert body["cnpj"] == "19131243000197"
        assert body["razao_social"] == "OPEN KNOWLEDGE BRASIL"

    @respx.mock
    def test_company_all_apis_down_returns_empty(self, client):
        respx.get("https://minhareceita.org/19131243000197").mock(
            return_value=httpx.Response(500, text="erro")
        )
        respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            return_value=httpx.Response(500, text="erro")
        )
        respx.get("https://www.receitaws.com.br/v1/cnpj/19131243000197").mock(
            return_value=httpx.Response(500, text="erro")
        )

        r = client.get("/api/companies/19131243000197")
        assert r.status_code == 200
        body = r.json()
        # Estrutura minima, sem razao social
        assert body["razao_social"] is None
        # Fontes registradas com erro
        assert len(body["fontes"]) >= 3


class TestHistory:
    def test_history_empty(self, client):
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
