"""CampaignRegistry — registro persistente de campanhas e suas imagens.

Espelha o ProductRegistry (SQLite, transacional). Responsável por:
- gerar campaign_id sequencial humano: C000001, C000002, ...
- registrar imagens de marketing por campanha com sequencial próprio
  (001, 002, ...) — estrutura já preparada para múltiplas imagens
  por campanha;
- dar idempotência: a mesma (campaign_id, image_seq) nunca é
  registrada duas vezes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_images (
    campaign_id TEXT NOT NULL,
    image_seq INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    drive_file_id TEXT,
    drive_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, image_seq)
);
CREATE TABLE IF NOT EXISTS daily_runs (
    run_date TEXT PRIMARY KEY,
    product_id TEXT,
    campaign_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CampaignRegistry:
    def __init__(self, db_path: str | Path = "data/campaign_registry.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- IDs sequenciais -------------------------------------------------

    def next_campaign_id(self) -> str:
        """Próximo ID sequencial no formato C000001."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES ('campaign_seq', 0) "
                "ON CONFLICT(name) DO NOTHING"
            )
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'campaign_seq'"
            )
            (value,) = self._conn.execute(
                "SELECT value FROM counters WHERE name = 'campaign_seq'"
            ).fetchone()
        return f"C{value:06d}"

    # --- Campanhas -------------------------------------------------------

    def create(
        self,
        campaign_id: str,
        product_id: str,
        fingerprint: str | None = None,
        status: str = "draft",
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Registra a campanha. Idempotente: campaign_id existente retorna
        o registro atual sem alterar."""
        existing = self.get(campaign_id)
        if existing:
            return existing
        seq = int(campaign_id[1:]) if campaign_id.startswith("C") else 0
        with self._conn:
            self._conn.execute(
                "INSERT INTO campaigns (campaign_id, seq, product_id, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, seq, product_id, status, _now(), _now()),
            )
        if fingerprint:
            self.update_fingerprint(campaign_id, fingerprint)
        if content:
            self.update_content(campaign_id, content)
        return self.get(campaign_id)

    def update_content(self, campaign_id: str, content: dict[str, Any]) -> None:
        """Grava o conteúdo completo da campanha (JSON) — necessário para
        /produto, /republicar e para a aprovação sobreviver a restarts."""
        self._ensure_column("campaigns", "content_json", "TEXT")
        with self._conn:
            self._conn.execute(
                "UPDATE campaigns SET content_json = ?, updated_at = ? "
                "WHERE campaign_id = ?",
                (json.dumps(content, ensure_ascii=False), _now(), campaign_id),
            )

    def get_content(self, campaign_id: str) -> dict[str, Any] | None:
        self._ensure_column("campaigns", "content_json", "TEXT")
        row = self._conn.execute(
            "SELECT content_json FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if not row or not row["content_json"]:
            return None
        return json.loads(row["content_json"])

    def latest_for_product(self, product_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM campaigns WHERE product_id = ? ORDER BY seq DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- Aprovação humana (AUTO_PUBLISH=false) ----------------------------

    def create_approval(
        self,
        campaign_id: str,
        telegram_chat_id: int,
        decision_options: list[str] | None = None,
    ) -> dict[str, Any]:
        """Registra uma campanha aguardando decisão humana no Telegram.

        Idempotente: se já existe aprovação PENDENTE para a campanha,
        retorna a existente (reenvio da prévia não cria outra)."""
        self._ensure_approvals_table()
        existing = self.get_pending_approval(chat_id=telegram_chat_id)
        if existing and existing["campaign_id"] == campaign_id:
            return existing
        options = json.dumps(decision_options or ["PUBLICAR", "CANCELAR", "REFAZER"])
        with self._conn:
            self._conn.execute(
                "INSERT INTO campaign_approvals (campaign_id, telegram_chat_id, "
                "status, decision_options, created_at) VALUES (?, ?, 'pending', ?, ?)",
                (campaign_id, telegram_chat_id, options, _now()),
            )
        return self.get_pending_approval(chat_id=telegram_chat_id)

    def get_pending_approval(
        self, chat_id: int | None = None, campaign_id: str | None = None
    ) -> dict[str, Any] | None:
        """Aprovação pendente mais recente — por chat (decisão da prévia)
        ou por campanha."""
        self._ensure_approvals_table()
        if campaign_id:
            row = self._conn.execute(
                "SELECT * FROM campaign_approvals WHERE campaign_id = ? "
                "AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (campaign_id,),
            ).fetchone()
        elif chat_id is not None:
            row = self._conn.execute(
                "SELECT * FROM campaign_approvals WHERE telegram_chat_id = ? "
                "AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        else:
            return None
        return dict(row) if row else None

    def resolve_approval(
        self, approval_id: int, status: str, decision: str | None = None
    ) -> dict[str, Any]:
        """Fecha a aprovação: status em published | cancelled | redone."""
        self._ensure_approvals_table()
        with self._conn:
            self._conn.execute(
                "UPDATE campaign_approvals SET status = ?, decision = ?, "
                "decided_at = ? WHERE id = ?",
                (status, decision, _now(), approval_id),
            )
        row = self._conn.execute(
            "SELECT * FROM campaign_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row)

    def _ensure_approvals_table(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT,
                    decision_options TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                )
                """
            )

    def update_fingerprint(self, campaign_id: str, fingerprint: str) -> None:
        """Grava o fingerprint do conteúdo (coluna criada sob demanda —
        bancos antigos não a têm; ALTER TABLE é seguro/idempotente)."""
        self._ensure_column("campaigns", "fingerprint", "TEXT")
        with self._conn:
            self._conn.execute(
                "UPDATE campaigns SET fingerprint = ?, updated_at = ? "
                "WHERE campaign_id = ?",
                (fingerprint, _now(), campaign_id),
            )

    def recent_campaigns(
        self, product_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Campanhas recentes do produto (para o anti-duplicação)."""
        self._ensure_column("campaigns", "fingerprint", "TEXT")
        rows = self._conn.execute(
            "SELECT * FROM campaigns WHERE product_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Rotina diária (FASE 8) --------------------------------------------

    def start_daily_run(
        self, run_date: str, *, reason: str, product_id: str | None = None
    ) -> bool:
        """Tenta iniciar a execução diária de `run_date`.

        Retorna True se esta chamada venceu a corrida (run registrada em
        'running'); False se o dia já tinha sido processado — proteção
        contra duplicação da rotina 08:00 (retry do n8n, reexecução
        manual, dois gatilhos).
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO daily_runs (run_date, product_id, reason, "
                "status, created_at) VALUES (?, ?, ?, 'running', ?)",
                (run_date, product_id, reason, _now()),
            )
            if cursor.rowcount:
                return True
            row = self._conn.execute(
                "SELECT status FROM daily_runs WHERE run_date = ?", (run_date,)
            ).fetchone()
            if row and row["status"] == "running":
                # run anterior morreu no meio (crash) — retomada manual
                self._conn.execute(
                    "UPDATE daily_runs SET status = 'running', reason = ?, "
                    "product_id = COALESCE(?, product_id), created_at = ? "
                    "WHERE run_date = ?",
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
        with self._conn:
            self._conn.execute(
                "UPDATE daily_runs SET status = ?, detail = ?, "
                "product_id = COALESCE(?, product_id), "
                "campaign_id = COALESCE(?, campaign_id) WHERE run_date = ?",
                (status, detail, product_id, campaign_id, run_date),
            )

    def get_daily_run(self, run_date: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM daily_runs WHERE run_date = ?", (run_date,)
        ).fetchone()
        return dict(row) if row else None

    def list_daily_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM daily_runs ORDER BY run_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- internos -------------------------------------------------------------

    def _ensure_column(self, table: str, column: str, ddl_type: str) -> None:
        cols = {
            r[1]
            for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in cols:
            with self._conn:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
                )

    def get(self, campaign_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_status(self, campaign_id: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE campaigns SET status = ?, updated_at = ? WHERE campaign_id = ?",
                (status, _now(), campaign_id),
            )

    # --- Imagens de marketing ---------------------------------------------

    def next_image_seq(self, campaign_id: str) -> int:
        """Próximo sequencial de imagem da campanha (1-based)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(image_seq), 0) + 1 FROM campaign_images "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        return int(row[0])

    def register_image(
        self,
        campaign_id: str,
        image_seq: int,
        file_name: str,
        drive_file_id: str | None = None,
        drive_url: str | None = None,
        status: str = "generated",
    ) -> dict[str, Any]:
        """Registra (ou atualiza, via upsert) uma imagem da campanha."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO campaign_images (campaign_id, image_seq, file_name, "
                "drive_file_id, drive_url, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id, image_seq) DO UPDATE SET "
                "file_name = excluded.file_name, "
                "drive_file_id = excluded.drive_file_id, "
                "drive_url = excluded.drive_url, "
                "status = excluded.status",
                (campaign_id, image_seq, file_name, drive_file_id, drive_url,
                 status, _now()),
            )
        return self.get_image(campaign_id, image_seq)

    def get_image(self, campaign_id: str, image_seq: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM campaign_images WHERE campaign_id = ? AND image_seq = ?",
            (campaign_id, image_seq),
        ).fetchone()
        return dict(row) if row else None

    def list_images(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM campaign_images WHERE campaign_id = ? ORDER BY image_seq",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]
