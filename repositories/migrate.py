"""Runner de migrations PostgreSQL.

Aplica os arquivos `migrations/NNN_nome.sql` em ordem, registrando cada
versão aplicada em `schema_migrations`. Idempotente: versões já
aplicadas são puladas.

CLI:
    python -m repositories.migrate            # aplica pendentes
    python -m repositories.migrate --status   # lista aplicadas/pendentes
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _available() -> list[tuple[int, str, Path]]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"(\d+)_(.+)\.sql$", path.name)
        if match:
            migrations.append((int(match.group(1)), match.group(2), path))
    return sorted(migrations)


def run_migrations(database_url: str) -> list[int]:
    """Aplica as migrations pendentes. Retorna as versões aplicadas agora."""
    import psycopg

    applied_now: list[int] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT        NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {
            r[0]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, path in _available():
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) "
                    "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                    (version, name),
                )
            applied_now.append(version)
    return applied_now


def status(database_url: str) -> dict:
    import psycopg

    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT        NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {
            r[0]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
    return {
        "applied": sorted(done),
        "pending": [v for v, _, _ in _available() if v not in done],
    }


if __name__ == "__main__":
    import os

    url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else os.environ.get(
        "DATABASE_URL", ""
    )
    if not url:
        sys.exit("DATABASE_URL não configurada (use --url ou a env var).")
    if "--status" in sys.argv:
        print(status(url))
    else:
        applied = run_migrations(url)
        print(f"migrations aplicadas: {applied or 'nenhuma pendente'}")
