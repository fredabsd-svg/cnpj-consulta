"""Interface comum para provedores de dados de CNPJ.

Cada provedor concreto herda de Provider e implementa fetch_company.
Provedores sao stateless em relacao aos dados -- toda configuracao vem do
Settings. O unico estado mantido e o de saude (ProviderStatus) e a janela
do limitador de taxa, ambos por instancia (o registry e um singleton).
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app import __version__
from app.config import Settings

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Erro generico de provedor."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
        source: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.source = source


class RateLimitedError(ProviderError):
    """Consulta nao enviada: o limite local de requisicoes foi atingido."""


class RateLimiter:
    """Janela deslizante de N requisicoes por 60 segundos.

    Nao bloqueia: `reserve()` devolve 0 quando ha vaga (e ja consome a
    vaga) ou quantos segundos faltam ate a proxima vaga. Assim uma fonte
    lenta/limitada nunca segura a resposta das demais.
    """

    WINDOW = 60.0

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: deque[float] = deque()
        self._blocked_until = 0.0

    def _purge(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= self.WINDOW:
            self._hits.popleft()

    def reserve(self) -> float:
        now = time.monotonic()
        if now < self._blocked_until:
            return self._blocked_until - now
        self._purge(now)
        if len(self._hits) >= self.per_minute:
            return self.WINDOW - (now - self._hits[0])
        self._hits.append(now)
        return 0.0

    def block_for(self, seconds: float) -> None:
        """Chamado quando o servidor responde 429: pausa a fonte."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)


@dataclass
class ProviderStatus:
    """Saude atual do provedor."""

    name: str
    enabled: bool
    last_success: datetime | None = None
    last_failure: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    total_queries: int = 0
    total_errors: int = 0


@dataclass
class ProviderResult:
    """Resposta bruta de um provedor antes da normalizacao.

    Cada provedor retorna dados no formato ProviderResult, e o servico de
    conciliacao combina varios ProviderResult em um CompanyUnified.
    """

    provider: str
    raw: dict[str, Any]
    http_status: int
    url: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    is_mirror_of_rfb: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    value = resp.headers.get("Retry-After", "")
    try:
        return max(1.0, float(value))
    except ValueError:
        return default


class Provider(abc.ABC):
    """Interface abstrata de provedor."""

    name: str = "base"
    is_mirror_of_rfb: bool = False  # se True, nao conta como fonte independente

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owned_client = client is None
        self._status = ProviderStatus(name=self.name, enabled=self.enabled)
        self._limiter = RateLimiter(self.rate_limit_per_minute)

    # ---------- Configuracao por subclasse ----------

    @property
    @abc.abstractmethod
    def enabled(self) -> bool: ...

    @property
    @abc.abstractmethod
    def base_url(self) -> str: ...

    def build_url(self, cnpj: str) -> str | None:
        """URL publica consultada (exibida na tabela de procedencia)."""
        return None

    @property
    def timeout(self) -> float:
        return float(self.settings.request_timeout_seconds)

    @property
    def max_retries(self) -> int:
        return self.settings.request_max_retries

    @property
    def backoff(self) -> float:
        return self.settings.request_backoff_seconds

    @property
    def rate_limit_per_minute(self) -> int:
        return 60

    @property
    def needs_api_key(self) -> bool:
        return False

    @property
    def status(self) -> ProviderStatus:
        return self._status

    # ---------- HTTP com limite de taxa e retry ----------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"Accept": "application/json", "User-Agent": f"cnpj-consulta/{__version__}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _take_slot(self) -> None:
        wait = self._limiter.reserve()
        if wait > 0:
            raise RateLimitedError(
                f"limite de {self.rate_limit_per_minute} consultas/min atingido; "
                f"pulada nesta consulta (libera em {wait:.0f}s)",
                status=429,
                transient=True,
                source=self.name,
            )

    async def _request_json(self, url: str) -> tuple[int, dict[str, Any]]:
        """GET com limite de taxa e retry exponencial. Retorna (status, body).

        - 429: NAO repete (repetir so gasta a cota); pausa a fonte pelo
          tempo do header Retry-After (ou 60s) e devolve 429.
        - 5xx / erro de rede: repete ate `max_retries` com backoff.
          O slot de rate limit e consumido so na 1a tentativa -- retries
          de rede/5xx nao queimam a cota local.
        """
        for attempt in range(self.max_retries + 1):
            if attempt == 0:
                self._take_slot()
            try:
                client = await self._get_client()
                resp = await client.get(url)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < self.max_retries:
                    wait = self.backoff * (2**attempt)
                    log.warning("[%s] erro de rede (%s); nova tentativa em %.1fs", self.name, e, wait)
                    await asyncio.sleep(wait)
                    continue
                raise
            if resp.status_code == 429:
                self._limiter.block_for(_retry_after_seconds(resp, RateLimiter.WINDOW))
                log.warning("[%s] HTTP 429 (limite do servidor)", self.name)
                return 429, {}
            if resp.status_code >= 500 and attempt < self.max_retries:
                wait = self.backoff * (2**attempt)
                log.warning("[%s] HTTP %s; nova tentativa em %.1fs", self.name, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {"_raw": resp.text[:500]}
            if not isinstance(body, dict):
                body = {"_raw": body}
            return resp.status_code, body
        return 0, {}  # inalcancavel: o laco sempre retorna ou levanta

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Traduz status HTTP em mensagens curtas para o usuario.

        As mensagens sao sempre exibidas ao lado do nome da fonte, por isso
        nao repetem o nome.
        """
        if status == 404:
            raise ProviderError("CNPJ nao encontrado nesta fonte", status=404, source=self.name)
        if status == 429:
            raise ProviderError(
                "a fonte recusou por excesso de consultas (rate limit); tente em 1 minuto",
                status=429,
                transient=True,
                source=self.name,
            )
        if status == 400:
            raise ProviderError("a fonte recusou o CNPJ (HTTP 400)", status=400, source=self.name)
        if status >= 400:
            raise ProviderError(
                f"fonte indisponivel (HTTP {status})", status=status, transient=status >= 500, source=self.name
            )

    # ---------- API publica ----------

    @abc.abstractmethod
    async def fetch_company(self, cnpj: str) -> ProviderResult:
        """Busca os dados da empresa pelo CNPJ normalizado."""
        raise NotImplementedError

    # ---------- Helpers para subclasses ----------

    async def _timed(self, coro):
        """Atualiza o status de saude conforme sucesso/falha."""
        try:
            r = await coro
        except Exception as e:
            self._status.last_failure = datetime.now(UTC)
            self._status.consecutive_failures += 1
            self._status.last_error = str(e)[:200]
            self._status.total_errors += 1
            self._status.total_queries += 1
            raise
        self._status.last_success = datetime.now(UTC)
        self._status.consecutive_failures = 0
        self._status.total_queries += 1
        return r
