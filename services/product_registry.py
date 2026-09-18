"""Registro local de produtos (SQLite).

Dois papéis:

1. **Idempotência de intake** — mapa `(telegram_chat_id, message_id)`
   para `product_id`. Se a mesma mensagem do Telegram chegar duas
   vezes (retry do n8n, reenvio do Telegram), respondemos com o
   produto já criado em vez de criar outro.
2. **IDs sequenciais humanos** — `P000001`, `P000002`, ... usados
   como chave do produto no Sheets e no nome do arquivo no Drive
   (`P000001_camisa_feminina_original.jpg`).

É separado do checkpointer do LangGraph de propósito: o checkpointer
guarda o estado da conversa; este registro guarda o efeito de
negócio (produto criado) — que não pode ser duplicado nem quando a
conversa é refeita.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    telegram_chat_id INTEGER,
    message_id INTEGER,
    name TEXT,
    drive_file_id TEXT,
    drive_url TEXT,
    sheet_row INTEGER,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (telegram_chat_id, message_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductRegistry:
    def __init__(self, db_path: str | Path = "data/product_registry.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- IDs sequenciais -------------------------------------------------

    def next_product_id(self) -> str:
        """Próximo ID sequencial no formato P000001."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES ('product_seq', 0) "
                "ON CONFLICT(name) DO NOTHING"
            )
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'product_seq'"
            )
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name = 'product_seq'"
            ).fetchone()
        return f"P{row['value']:06d}"

    # --- Idempotência de intake ------------------------------------------

    def find_by_message(
        self, telegram_chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM products WHERE telegram_chat_id = ? AND message_id = ?",
            (telegram_chat_id, message_id),
        ).fetchone()
        return dict(row) if row else None

    # --- CRUD --------------------------------------------------------------

    def create(
        self,
        *,
        product_id: str,
        telegram_chat_id: int | None = None,
        message_id: int | None = None,
        name: str | None = None,
    ) -> None:
        """Registra o produto. Idempotente: se já existe, não faz nada."""
        if self.get(product_id):
            return
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO products
                    (product_id, seq, telegram_chat_id, message_id, name,
                     created_at, updated_at)
                VALUES (
                    ?,
                    COALESCE((SELECT MAX(seq) FROM products), 0) + 1,
                    ?, ?, ?, ?, ?
                )
                """,
                (product_id, telegram_chat_id, message_id, name, _now(), _now()),
            )

    def update(self, product_id: str, **fields: Any) -> None:
        allowed = {"name", "drive_file_id", "drive_url", "sheet_row", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with self._conn:
            self._conn.execute(
                f"UPDATE products SET {assignments} WHERE product_id = ?",
                (*updates.values(), product_id),
            )

    def get(self, product_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        return dict(row) if row else None

    def latest(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM products ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
