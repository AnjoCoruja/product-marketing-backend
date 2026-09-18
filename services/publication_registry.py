"""PublicationRegistry — registro persistente das publicações sociais.

SQLite transacional, mesma filosofia do ProductRegistry/CampaignRegistry.
Chave de idempotência: (campaign_id, platform) — publicar duas vezes a
mesma campanha na mesma rede nunca cria dois posts nem dois registros.

Guarda exatamente o contrato exigido: platform, post_id, published_at,
status, error (+ caption usada e nº de tentativas).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    campaign_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    post_id TEXT,
    published_at TEXT,
    error TEXT,
    caption TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, platform)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PublicationRegistry:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            import os
            db_path = os.environ.get(
                "PUBLICATION_REGISTRY_DB", "data/publication_registry.db"
            )
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(
        self,
        campaign_id: str,
        platform: str,
        *,
        status: str,
        post_id: str | None = None,
        published_at: str | None = None,
        error: str | None = None,
        caption: str = "",
        increment_attempts: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO publications (campaign_id, platform, status, post_id, "
                "published_at, error, caption, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id, platform) DO UPDATE SET "
                "status = excluded.status, "
                "post_id = COALESCE(excluded.post_id, publications.post_id), "
                "published_at = COALESCE(excluded.published_at, publications.published_at), "
                "error = excluded.error, "
                "caption = CASE WHEN excluded.caption != '' THEN excluded.caption "
                "ELSE publications.caption END, "
                "attempts = publications.attempts + ?, "
                "updated_at = excluded.updated_at",
                (
                    campaign_id, platform, status, post_id, published_at, error,
                    caption, 1 if increment_attempts else 0, now, now,
                    1 if increment_attempts else 0,
                ),
            )
        return self.get(campaign_id, platform)

    def get(self, campaign_id: str, platform: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM publications WHERE campaign_id = ? AND platform = ?",
            (campaign_id, platform),
        ).fetchone()
        return dict(row) if row else None

    def list_by_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM publications WHERE campaign_id = ? ORDER BY platform",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def already_published(self, campaign_id: str, platform: str) -> bool:
        rec = self.get(campaign_id, platform)
        return bool(rec and rec["status"] == "published" and rec["post_id"])
