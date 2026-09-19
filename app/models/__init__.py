"""Modelos SQLAlchemy do banco principal (SQLite).

Tabelas:
- history: historico de consultas
- favorite: CNPJs favoritos
- cached_query: cache de respostas por provedor
"""

from app.models.cached_query import CachedQuery  # noqa: F401
from app.models.favorite import Favorite  # noqa: F401
from app.models.history import History  # noqa: F401
