"""Adaptadores de provedores de dados de CNPJ.

Cada provedor implementa a interface Provider (ver base.py).
"""

from app.providers.base import (  # noqa: F401
    Provider,
    ProviderError,
    ProviderResult,
    ProviderStatus,
)
from app.providers.registry import get_registry  # noqa: F401
