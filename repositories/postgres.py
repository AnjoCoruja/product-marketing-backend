"""Implementações PostgreSQL das interfaces Repository.

Uma conexão psycopg por repositório (check_same_thread equivalente via
lock interno do psycopg por conexão; os serviços compartilham a mesma
fábrica mas cada repo tem sua conexão). Todas as escritas são
transacionais (`with conn.transaction()`).

Comportamento espelha 1:1 os registries SQLite — incluindo a semântica
de transições de estado do OperationRegistry — para que nenhum agente
precise mudar.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _PgBase:
    """Base: conexão compartilhada com lock (chamadas do FastAPI são
    multi-thread; uma conexão por repositório serializa por escopo)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = dict_row
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock, self._conn.transaction():
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute(sql, params)


class PgProductRepository(_PgBase):
    """Implementa ProductRepository sobre a tabela `products`."""

    def next_product_id(self) -> str:
        with self._lock, self._conn.transaction():
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES ('product_seq', 0) "
                "ON CONFLICT (name) DO NOTHING"
            )
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'product_seq'"
            )
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = 'product_seq'"
            ).fetchone()
        return f"P{row['value']:06d}"

    def find_by_message(
        self, telegram_chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM products WHERE telegram_chat_id = %s AND message_id = %s",
            (telegram_chat_id, message_id),
        )

    def create(
        self,
        *,
        product_id: str,
        telegram_chat_id: int | None = None,
        message_id: int | None = None,
        name: str | None = None,
    ) -> None:
        if self.get(product_id):
            return
        with self._lock, self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO products
                    (product_id, seq, telegram_chat_id, message_id, name)
                VALUES (
                    %s,
                    COALESCE((SELECT MAX(seq) FROM products), 0) + 1,
                    %s, %s, %s
                )
                """,
                (product_id, telegram_chat_id, message_id, name),
            )

    def update(self, product_id: str, **fields: Any) -> None:
        allowed = {"name", "drive_file_id", "drive_url", "sheet_row", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{k} = %s" for k in updates)
        self._exec(
            f"UPDATE products SET {assignments} WHERE product_id = %s",
            (*updates.values(), product_id),
        )

    def get(self, product_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM products WHERE product_id = %s", (product_id,)
        )

    def latest(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM products ORDER BY seq DESC LIMIT 1")


class PgCampaignRepository(_PgBase):
    """Implementa CampaignRepository (campaigns + images + approvals +
    daily_runs)."""

    def next_campaign_id(self) -> str:
        with self._lock, self._conn.transaction():
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES ('campaign_seq', 0) "
                "ON CONFLICT (name) DO NOTHING"
            )
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'campaign_seq'"
            )
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = 'campaign_seq'"
            ).fetchone()
        return f"C{row['value']:06d}"

    def create(
        self,
        campaign_id: str,
        product_id: str,
        fingerprint: str | None = None,
        status: str = "draft",
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.get(campaign_id)
        if existing:
            return existing
        seq = int(campaign_id[1:]) if campaign_id.startswith("C") else 0
        with self._lock, self._conn.transaction():
            self._conn.execute(
                "INSERT INTO campaigns (campaign_id, seq, product_id, status, "
                "fingerprint, content) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    campaign_id, seq, product_id, status, fingerprint,
                    json.dumps(content, ensure_ascii=False)
                    if content is not None else None,
                ),
            )
        return self.get(campaign_id)

    def get(self, campaign_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM campaigns WHERE campaign_id = %s", (campaign_id,)
        )

    def update_status(self, campaign_id: str, status: str) -> None:
        self._exec(
            "UPDATE campaigns SET status = %s, updated_at = %s "
            "WHERE campaign_id = %s",
            (status, _now(), campaign_id),
        )

    def update_fingerprint(self, campaign_id: str, fingerprint: str) -> None:
        self._exec(
            "UPDATE campaigns SET fingerprint = %s, updated_at = %s "
            "WHERE campaign_id = %s",
            (fingerprint, _now(), campaign_id),
        )

    def update_content(self, campaign_id: str, content: dict[str, Any]) -> None:
        self._exec(
            "UPDATE campaigns SET content = %s, updated_at = %s "
            "WHERE campaign_id = %s",
            (json.dumps(content, ensure_ascii=False), _now(), campaign_id),
        )

    def get_content(self, campaign_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT content FROM campaigns WHERE campaign_id = %s", (campaign_id,)
        )
        if not row or not row["content"]:
            return None
        return row["content"]

    def latest_for_product(self, product_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM campaigns WHERE product_id = %s "
            "ORDER BY seq DESC LIMIT 1",
            (product_id,),
        )

    def recent_campaigns(
        self, product_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM campaigns WHERE product_id = %s "
            "ORDER BY seq DESC LIMIT %s",
            (product_id, limit),
        )

    # --- Aprovação humana -------------------------------------------------

    def create_approval(
        self,
        campaign_id: str,
        telegram_chat_id: int,
        decision_options: list[str] | None = None,
    ) -> dict[str, Any]:
        existing = self.get_pending_approval(chat_id=telegram_chat_id)
        if existing and existing["campaign_id"] == campaign_id:
            return existing
        options = json.dumps(decision_options or ["PUBLICAR", "CANCELAR", "REFAZER"])
        self._exec(
            "INSERT INTO campaign_approvals (campaign_id, telegram_chat_id, "
            "status, decision_options) VALUES (%s, %s, 'pending', %s)",
            (campaign_id, telegram_chat_id, options),
        )
        return self.get_pending_approval(chat_id=telegram_chat_id)

    def get_pending_approval(
        self, chat_id: int | None = None, campaign_id: str | None = None
    ) -> dict[str, Any] | None:
        if campaign_id:
            return self._one(
                "SELECT * FROM campaign_approvals WHERE campaign_id = %s "
                "AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (campaign_id,),
            )
        if chat_id is not None:
            return self._one(
                "SELECT * FROM campaign_approvals WHERE telegram_chat_id = %s "
                "AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            )
        return None

    def resolve_approval(
        self, approval_id: int, status: str, decision: str | None = None
    ) -> dict[str, Any]:
        self._exec(
            "UPDATE campaign_approvals SET status = %s, decision = %s, "
            "decided_at = %s WHERE id = %s",
            (status, decision, _now(), approval_id),
        )
        row = self._one(
            "SELECT * FROM campaign_approvals WHERE id = %s", (approval_id,)
        )
        return row

    # --- Imagens ------------------------------------------------------------

    def next_image_seq(self, campaign_id: str) -> int:
        row = self._one(
            "SELECT COALESCE(MAX(image_seq), 0) + 1 AS next FROM campaign_images "
            "WHERE campaign_id = %s",
            (campaign_id,),
        )
        return int(row["next"])

    def register_image(
        self,
        campaign_id: str,
        image_seq: int,
        file_name: str,
        drive_file_id: str | None = None,
        drive_url: str | None = None,
        status: str = "generated",
    ) -> dict[str, Any]:
        self._exec(
            "INSERT INTO campaign_images (campaign_id, image_seq, file_name, "
            "drive_file_id, drive_url, status) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (campaign_id, image_seq) DO UPDATE SET "
            "file_name = EXCLUDED.file_name, "
            "drive_file_id = EXCLUDED.drive_file_id, "
            "drive_url = EXCLUDED.drive_url, "
            "status = EXCLUDED.status",
            (campaign_id, image_seq, file_name, drive_file_id, drive_url, status),
        )
        return self.get_image(campaign_id, image_seq)

    def get_image(
        self, campaign_id: str, image_seq: int
    ) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM campaign_images WHERE campaign_id = %s AND image_seq = %s",
            (campaign_id, image_seq),
        )

    def list_images(self, campaign_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM campaign_images WHERE campaign_id = %s ORDER BY image_seq",
            (campaign_id,),
        )

    # --- Rotina diária ------------------------------------------------------

    def start_daily_run(
        self, run_date: str, *, reason: str, product_id: str | None = None
    ) -> bool:
        with self._lock, self._conn.transaction():
            cur = self._conn.execute(
                "INSERT INTO daily_runs (run_date, product_id, reason, status) "
                "VALUES (%s, %s, %s, 'running') ON CONFLICT (run_date) DO NOTHING",
                (run_date, product_id, reason),
            )
            if cur.rowcount:
                return True
            row = self._conn.execute(
                "SELECT status FROM daily_runs WHERE run_date = %s", (run_date,)
            ).fetchone()
            if row and row["status"] == "running":
                # run anterior morreu no meio (crash) — retomada manual
                self._conn.execute(
                    "UPDATE daily_runs SET status = 'running', reason = %s, "
                    "product_id = COALESCE(%s, product_id), created_at = %s "
                    "WHERE run_date = %s",
                    (reason, product_id, _now(), run_date),
                )
                return True
            return False

    def finish_daily_run(
        self,
        run_date: str,
        *,
        status: str,
        detail: str = "",
        product_id: str | None = None,
        campaign_id: str | None = None,
    ) -> None:
        self._exec(
            "UPDATE daily_runs SET status = %s, detail = %s, "
            "product_id = COALESCE(%s, product_id), "
            "campaign_id = COALESCE(%s, campaign_id) WHERE run_date = %s",
            (status, detail, product_id, campaign_id, run_date),
        )

    def get_daily_run(self, run_date: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM daily_runs WHERE run_date = %s", (run_date,)
        )

    def list_daily_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM daily_runs ORDER BY run_date DESC LIMIT %s", (limit,)
        )


class PgSocialPostRepository(_PgBase):
    """Implementa SocialPostRepository sobre `social_posts`."""

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
        self._exec(
            """
            INSERT INTO social_posts
                (campaign_id, platform, status, post_id, published_at, error,
                 caption, attempts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (campaign_id, platform) DO UPDATE SET
                status = EXCLUDED.status,
                post_id = COALESCE(EXCLUDED.post_id, social_posts.post_id),
                published_at = COALESCE(EXCLUDED.published_at,
                                        social_posts.published_at),
                error = EXCLUDED.error,
                caption = CASE WHEN EXCLUDED.caption != ''
                               THEN EXCLUDED.caption
                               ELSE social_posts.caption END,
                attempts = social_posts.attempts + %s,
                updated_at = %s
            """,
            (
                campaign_id, platform, status, post_id, published_at, error,
                caption, 1 if increment_attempts else 0,
                1 if increment_attempts else 0, now,
            ),
        )
        return self.get(campaign_id, platform)

    def get(self, campaign_id: str, platform: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM social_posts WHERE campaign_id = %s AND platform = %s",
            (campaign_id, platform),
        )

    def list_by_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM social_posts WHERE campaign_id = %s ORDER BY platform",
            (campaign_id,),
        )

    def already_published(self, campaign_id: str, platform: str) -> bool:
        rec = self.get(campaign_id, platform)
        return bool(rec and rec["status"] == "published" and rec["post_id"])
