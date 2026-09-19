"""Testes do polish pos-PR #1: config morta, bordas e mensagens."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.core.inbound_limit import InboundRateLimiter, reset_inbound_limiter
from app.main import app
from app.services.partner_search import companies_of_partner_document


class TestDeadConfigRemoved:
    def test_cnpjws_api_key_nao_existe_mais(self):
        """Chave nunca foi usada pelo adaptador; removida limpa."""
        assert "cnpjws_api_key" not in Settings.model_fields
        settings = get_settings()
        assert not hasattr(settings, "cnpjws_api_key")


class TestPartnerSearchEdges:
    def test_documento_vazio_retorna_lista_vazia_sem_tocar_db(self, monkeypatch):
        """Evita abrir a base local para entrada vazia."""

        def _boom() -> str:
            raise AssertionError("nao deveria resolver caminho da base")

        monkeypatch.setattr("app.services.partner_search._db_path", _boom)
        assert companies_of_partner_document("") == []
        assert companies_of_partner_document("   ") == []


class TestInboundLimiterMessage:
    def test_reserve_usa_per_minute_da_instancia(self):
        lim = InboundRateLimiter(1)
        assert lim.reserve("9.9.9.9") == 0.0
        wait = lim.reserve("9.9.9.9")
        assert wait > 0
        assert lim.per_minute == 1


class TestApiInvalidCnpjMessage:
    def test_companies_mensagem_clara(self, db_initialized):
        reset_inbound_limiter()
        client = TestClient(app)
        r = client.get("/api/companies/123")
        assert r.status_code == 400
        assert "14 caracteres" in r.json()["detail"]

    def test_partners_companies_mensagem_clara(self, db_initialized):
        client = TestClient(app)
        r = client.get("/api/partners/123/companies")
        assert r.status_code == 400
        assert "14 caracteres" in r.json()["detail"]
