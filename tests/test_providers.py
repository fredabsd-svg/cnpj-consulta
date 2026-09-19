"""Testes dos provedores (com mocks HTTPX)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import get_settings
from app.providers.base import RateLimitedError, RateLimiter
from app.providers.brasilapi import BrasilAPIProvider
from app.providers.minha_receita import MinhaReceitaProvider
from app.providers.receitaws import ReceitaWSProvider


class TestBrasilAPIProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_success(self, brasilapi_payload):
        respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            return_value=httpx.Response(200, json=brasilapi_payload)
        )
        provider = BrasilAPIProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client  # injeta client compartilhado
            result = await provider.fetch_company("19131243000197")

        assert result.provider == "brasilapi"
        assert result.http_status == 200
        assert result.is_mirror_of_rfb is True
        assert result.raw["razao_social"] == "OPEN KNOWLEDGE BRASIL"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_not_found(self):
        respx.get("https://brasilapi.com.br/api/cnpj/v1/00000000000000").mock(
            return_value=httpx.Response(404, json={"message": "nao encontrado"})
        )
        provider = BrasilAPIProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            with pytest.raises(RuntimeError, match="nao encontrado"):
                await provider.fetch_company("00000000000000")

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_timeout(self):
        respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        provider = BrasilAPIProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            with pytest.raises(httpx.TimeoutException):
                await provider.fetch_company("19131243000197")

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_500_retries_then_raises(self):
        route = respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            return_value=httpx.Response(500, text="erro")
        )
        settings = get_settings()
        provider = BrasilAPIProvider(settings)
        async with httpx.AsyncClient() as client:
            provider._client = client
            with pytest.raises(RuntimeError, match="500"):
                await provider.fetch_company("19131243000197")
        assert route.call_count == settings.request_max_retries + 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_is_not_retried_and_pauses_provider(self):
        route = respx.get("https://brasilapi.com.br/api/cnpj/v1/19131243000197").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        provider = BrasilAPIProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            with pytest.raises(RuntimeError, match="rate limit"):
                await provider.fetch_company("19131243000197")
            # Segunda consulta nem sai da maquina: a fonte esta em pausa
            with pytest.raises(RateLimitedError):
                await provider.fetch_company("19131243000197")
        assert route.call_count == 1


class TestRateLimiter:
    def test_blocks_after_limit(self):
        limiter = RateLimiter(per_minute=2)
        assert limiter.reserve() == 0
        assert limiter.reserve() == 0
        assert limiter.reserve() > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_receitaws_local_limit_skips_http(self, monkeypatch, receitaws_payload):
        monkeypatch.setenv("RATE_LIMIT_RECEITAWS", "1")
        get_settings.cache_clear()
        route = respx.get("https://www.receitaws.com.br/v1/cnpj/19131243000197").mock(
            return_value=httpx.Response(200, json=receitaws_payload)
        )
        provider = ReceitaWSProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            await provider.fetch_company("19131243000197")
            with pytest.raises(RateLimitedError, match="limite"):
                await provider.fetch_company("19131243000197")
        assert route.call_count == 1


class TestMinhaReceitaProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_success(self, brasilapi_payload):
        # Minha Receita tem mesmo schema
        respx.get("https://minhareceita.org/19131243000197").mock(
            return_value=httpx.Response(200, json=brasilapi_payload)
        )
        provider = MinhaReceitaProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            result = await provider.fetch_company("19131243000197")

        assert result.provider == "minha_receita"
        assert result.is_mirror_of_rfb is False  # FONTE PRIMARIA
        assert result.http_status == 200


class TestReceitaWSProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_success(self, receitaws_payload):
        respx.get("https://www.receitaws.com.br/v1/cnpj/19131243000197").mock(
            return_value=httpx.Response(200, json=receitaws_payload)
        )
        provider = ReceitaWSProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            result = await provider.fetch_company("19131243000197")

        assert result.provider == "receitaws"
        assert result.is_mirror_of_rfb is True
        assert result.raw["nome"] == "OPEN KNOWLEDGE BRASIL"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_company_rate_limit(self):
        respx.get("https://www.receitaws.com.br/v1/cnpj/19131243000197").mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )
        provider = ReceitaWSProvider(get_settings())
        async with httpx.AsyncClient() as client:
            provider._client = client
            with pytest.raises(RuntimeError, match="rate limit"):
                await provider.fetch_company("19131243000197")
