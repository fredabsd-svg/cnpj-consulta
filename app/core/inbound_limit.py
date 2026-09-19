"""Rate limit de entrada (por IP) para consultas de empresa.

Janela deslizante em memoria, sem dependencia externa. Suficiente para o
cenario single-process do uvicorn local; nao e distribuido.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import get_settings


class InboundRateLimiter:
    """N requisicoes por 60s, chaveadas por IP do cliente."""

    WINDOW = 60.0

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _purge(self, key: str, now: float) -> None:
        q = self._hits[key]
        while q and now - q[0] >= self.WINDOW:
            q.popleft()

    def reserve(self, key: str) -> float:
        """0 se ha vaga (e consome); senao segundos ate liberar."""
        now = time.monotonic()
        self._purge(key, now)
        q = self._hits[key]
        if len(q) >= self.per_minute:
            return self.WINDOW - (now - q[0])
        q.append(now)
        return 0.0


_limiter: InboundRateLimiter | None = None


def get_inbound_limiter() -> InboundRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = InboundRateLimiter(get_settings().inbound_rate_limit_per_minute)
    return _limiter


def reset_inbound_limiter() -> None:
    """Usado pelos testes para isolar estado."""
    global _limiter
    _limiter = None


def check_company_lookup_limit(request: Request) -> None:
    """Dependencia FastAPI: 429 se o IP excedeu o limite de consultas."""
    client = request.client.host if request.client else "unknown"
    wait = get_inbound_limiter().reserve(client)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Limite de consultas atingido ({get_settings().inbound_rate_limit_per_minute}/min). "
                f"Tente novamente em {wait:.0f}s."
            ),
            headers={"Retry-After": str(max(1, int(wait)))},
        )
