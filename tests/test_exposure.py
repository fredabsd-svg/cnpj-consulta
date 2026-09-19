"""Testes de protecao de exposicao (bind guard + rate limit de entrada)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, assert_bind_allowed
from app.core.inbound_limit import InboundRateLimiter, reset_inbound_limiter
from app.main import app


class TestBindGuard:
    def test_loopback_always_ok(self):
        assert_bind_allowed(Settings(app_host="127.0.0.1", app_env="production", app_debug=False))
        assert_bind_allowed(Settings(app_host="localhost", app_env="production", app_debug=False))
        assert_bind_allowed(Settings(app_host="::1", app_env="production", app_debug=False))

    def test_non_loopback_blocked_in_production(self):
        with pytest.raises(RuntimeError, match="nao e loopback"):
            assert_bind_allowed(
                Settings(app_host="0.0.0.0", app_env="production", app_debug=True)
            )

    def test_non_loopback_blocked_when_debug_false(self):
        with pytest.raises(RuntimeError, match="nao e loopback"):
            assert_bind_allowed(
                Settings(app_host="0.0.0.0", app_env="development", app_debug=False)
            )

    def test_non_loopback_allowed_in_dev_debug(self):
        # Laboratorio controlado: development + debug=true
        assert_bind_allowed(
            Settings(app_host="0.0.0.0", app_env="development", app_debug=True)
        )


class TestInboundLimiter:
    def test_reserve_blocks_after_budget(self):
        lim = InboundRateLimiter(2)
        assert lim.reserve("1.1.1.1") == 0.0
        assert lim.reserve("1.1.1.1") == 0.0
        assert lim.reserve("1.1.1.1") > 0
        # Outro IP independente
        assert lim.reserve("2.2.2.2") == 0.0

    def test_api_returns_429_when_exhausted(self, db_initialized, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("INBOUND_RATE_LIMIT_PER_MINUTE", "2")
        get_settings.cache_clear()
        reset_inbound_limiter()

        client = TestClient(app)
        assert client.get("/api/companies/123").status_code == 400  # CNPJ invalido, mas conta
        assert client.get("/api/companies/123").status_code == 400
        r = client.get("/api/companies/123")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
