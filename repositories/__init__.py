"""Camada Repository — persistência de produção em PostgreSQL.

O restante do sistema depende APENAS das interfaces (Protocols em
`base.py`), nunca do banco diretamente. As implementações concretas
são escolhidas pela factory `build_repositories()` conforme o ambiente:

    DATABASE_BACKEND=postgres + DATABASE_URL=postgresql://...  -> PostgreSQL
    ausente/dev/testes                                        -> SQLite legado

Módulos:
    base.py       — Protocols das 5 interfaces
    postgres.py   — implementações PostgreSQL (psycopg)
    factory.py    — construção conforme env + espelho opcional p/ Sheets
    migrate.py    — CLI de migrações (migrations/NNN_nome.sql)
"""

from .base import (
    AgentStateRepository,
    CampaignRepository,
    LogRepository,
    ProductRepository,
    SocialPostRepository,
)
from .factory import Repositories, build_repositories

__all__ = [
    "AgentStateRepository",
    "CampaignRepository",
    "LogRepository",
    "ProductRepository",
    "Repositories",
    "SocialPostRepository",
    "build_repositories",
]
