"""OperationRegistry — estado e dead-letter de todas as operações.

Estados do ciclo de vida:
    pending -> processing -> completed
                          -> failed     (esgotou tentativas -> dead-letter)
                          -> cancelled  (cancelamento manual)
    waiting_user          (pausa aguardando resposta humana no Telegram)

Cada operação registra EXATAMENTE onde falhou: service, operation (etapa),
attempts, error_type, error message e timestamps (created_at, started_at,
updated_at, finished_at).

Reprocessamento:
    retry(operation_id | product_id | campaign_id, from_stage=None)
    - uma operação `failed` volta para `pending` com correlation_id novo
      e attempt_count zerado — o chamador reexecuta a etapa que falhou;
    - operações `completed`/`cancelled` não são reprocessáveis.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from config import get_logger

logger = get_logger("service.operation_registry")

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "operations.db"
)


class OperationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    OperationStatus.PENDING: {
        OperationStatus.PROCESSING, OperationStatus.CANCELLED,
    },
    OperationStatus.PROCESSING: {
        OperationStatus.WAITING_USER, OperationStatus.COMPLETED,
        OperationStatus.FAILED, OperationStatus.CANCELLED,
    },
    OperationStatus.WAITING_USER: {
        OperationStatus.PROCESSING, OperationStatus.CANCELLED,
        OperationStatus.FAILED,
    },
    OperationStatus.FAILED: {
        OperationStatus.PENDING,   # reprocessamento
        OperationStatus.CANCELLED,
    },
    OperationStatus.COMPLETED: set(),
    OperationStatus.CANCELLED: set(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationRegistry:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get(
            "OPERATION_REGISTRY_DB", DEFAULT_DB_PATH
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id   TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    kind           TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    service        TEXT,
                    stage          TEXT,
                    product_id     TEXT,
                    campaign_id    TEXT,
                    attempt_count  INTEGER NOT NULL DEFAULT 0,
                    max_attempts   INTEGER NOT NULL DEFAULT 3,
                    error_type     TEXT,
                    error          TEXT,
                    dead_letter    INTEGER NOT NULL DEFAULT 0,
                    payload        TEXT,
                    result         TEXT,
                    created_at     TEXT NOT NULL,
                    started_at     TEXT,
                    updated_at     TEXT NOT NULL,
                    finished_at    TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_correlation "
                "ON operations(correlation_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_product "
                "ON operations(product_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_campaign "
                "ON operations(campaign_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_status "
                "ON operations(status)"
            )

    # ------------------------------------------------------------------

    def create(
        self,
        *,
        kind: str,
        service: str | None = None,
        stage: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        correlation_id: str | None = None,
        max_attempts: int = 3,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Registra uma operação nova em `pending`. Retorna o registro."""
        now = _now_iso()
        op = {
            "operation_id": str(uuid4()),
            "correlation_id": correlation_id or str(uuid4()),
            "kind": kind,
            "status": OperationStatus.PENDING.value,
            "service": service,
            "stage": stage,
            "product_id": product_id,
            "campaign_id": campaign_id,
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "error_type": None,
            "error": None,
            "dead_letter": 0,
            "payload": json.dumps(payload, ensure_ascii=False) if payload else None,
            "result": None,
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "finished_at": None,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operations (
                    operation_id, correlation_id, kind, status, service, stage,
                    product_id, campaign_id, attempt_count, max_attempts,
                    error_type, error, dead_letter, payload, result,
                    created_at, started_at, updated_at, finished_at
                ) VALUES (
                    :operation_id, :correlation_id, :kind, :status, :service,
                    :stage, :product_id, :campaign_id, :attempt_count,
                    :max_attempts, :error_type, :error, :dead_letter, :payload,
                    :result, :created_at, :started_at, :updated_at, :finished_at
                )
                """,
                op,
            )
        logger.info(
            "operação criada (%s)", kind,
            extra={
                "correlation_id": op["correlation_id"],
                "product_id": product_id,
                "campaign_id": campaign_id,
                "stage": stage,
            },
        )
        return op

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    # ------------------------------------------------------------------
    # transições de estado
    # ------------------------------------------------------------------

    def _transition(
        self,
        operation_id: str,
        target: OperationStatus,
        *,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Operação não encontrada: {operation_id}")
            current = OperationStatus(row["status"])
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise ValueError(
                    f"Transição inválida: {current.value} -> {target.value}"
                )
            fields = {"status": target.value, "updated_at": _now_iso()}
            fields.update(updates or {})
            assignments = ", ".join(f"{key} = :{key}" for key in fields)
            conn.execute(
                f"UPDATE operations SET {assignments} WHERE operation_id = :oid",
                {**fields, "oid": operation_id},
            )
        return self.get(operation_id)  # type: ignore[return-value]

    def start(self, operation_id: str) -> dict[str, Any]:
        """pending -> processing (marca started_at e incrementa tentativa)."""
        op = self.get(operation_id)
        if not op:
            raise KeyError(f"Operação não encontrada: {operation_id}")
        return self._transition(
            operation_id,
            OperationStatus.PROCESSING,
            updates={
                "started_at": op.get("started_at") or _now_iso(),
                "attempt_count": int(op["attempt_count"]) + 1,
            },
        )

    def mark_waiting_user(self, operation_id: str) -> dict[str, Any]:
        return self._transition(operation_id, OperationStatus.WAITING_USER)

    def complete(
        self, operation_id: str, *, result: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._transition(
            operation_id,
            OperationStatus.COMPLETED,
            updates={
                "finished_at": _now_iso(),
                "result": json.dumps(result, ensure_ascii=False) if result else None,
                "error": None,
                "error_type": None,
            },
        )

    def fail(
        self,
        operation_id: str,
        *,
        error: str,
        error_type: str | None = None,
        service: str | None = None,
        stage: str | None = None,
        dead_letter: bool = True,
    ) -> dict[str, Any]:
        """Marca a operação como failed, registrando EXATAMENTE onde falhou."""
        updates: dict[str, Any] = {
            "finished_at": _now_iso(),
            "error": error,
            "error_type": error_type,
            "dead_letter": 1 if dead_letter else 0,
        }
        if service:
            updates["service"] = service
        if stage:
            updates["stage"] = stage
        op = self._transition(operation_id, OperationStatus.FAILED, updates=updates)
        logger.error(
            "operação falhou em %s/%s: %s",
            op.get("service"), op.get("stage"), error,
            extra={
                "correlation_id": op["correlation_id"],
                "product_id": op.get("product_id"),
                "campaign_id": op.get("campaign_id"),
            },
        )
        return op

    def cancel(self, operation_id: str) -> dict[str, Any]:
        return self._transition(
            operation_id, OperationStatus.CANCELLED,
            updates={"finished_at": _now_iso()},
        )

    # ------------------------------------------------------------------
    # reprocessamento (/retry P000001)
    # ------------------------------------------------------------------

    def retry(
        self,
        ref: str,
        *,
        from_stage: str | None = None,
    ) -> dict[str, Any]:
        """Reprocessa a operação failed mais recente da referência.

        `ref` aceita: operation_id, product_id (P000001) ou campaign_id
        (C000001). A operação volta para `pending` com correlation_id NOVO
        (nova cadeia de logs) e attempt_count zerado.
        """
        op = self._resolve(ref)
        if not op:
            raise KeyError(f"Nenhuma operação encontrada para: {ref}")
        status = OperationStatus(op["status"])
        if status in (OperationStatus.COMPLETED, OperationStatus.CANCELLED):
            raise ValueError(
                f"Operação {op['operation_id']} está {status.value} — "
                "não reprocessável."
            )
        updates: dict[str, Any] = {
            "correlation_id": str(uuid4()),
            "attempt_count": 0,
            "error": None,
            "error_type": None,
            "dead_letter": 0,
            "finished_at": None,
        }
        if from_stage:
            updates["stage"] = from_stage
        new_op = self._transition(
            op["operation_id"], OperationStatus.PENDING, updates=updates
        )
        logger.info(
            "operação reenfileirada para reprocessamento",
            extra={
                "correlation_id": new_op["correlation_id"],
                "product_id": new_op.get("product_id"),
                "campaign_id": new_op.get("campaign_id"),
                "stage": new_op.get("stage"),
            },
        )
        return new_op

    def _resolve(self, ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (ref,)
            ).fetchone()
            if row:
                return self._decode(dict(row))
            row = conn.execute(
                """
                SELECT * FROM operations
                WHERE product_id = ? OR campaign_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (ref, ref),
            ).fetchone()
            if row:
                return self._decode(dict(row))
            # fallback: última operação FAILED da referência
            row = conn.execute(
                """
                SELECT * FROM operations
                WHERE (product_id = ? OR campaign_id = ?) AND status = 'failed'
                ORDER BY created_at DESC LIMIT 1
                """,
                (ref, ref),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    # ------------------------------------------------------------------
    # consultas / resumo
    # ------------------------------------------------------------------

    def list_dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM operations
                WHERE dead_letter = 1 AND status = 'failed'
                ORDER BY finished_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def list_by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operations WHERE correlation_id = ? "
                "ORDER BY created_at",
                (correlation_id,),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def list_by_status(
        self, status: OperationStatus, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operations WHERE status = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def summary(self) -> dict[str, Any]:
        """Resumo de execução: contagem por status/serviço + falhas recentes."""
        with self._connect() as conn:
            by_status = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM operations GROUP BY status"
                )
            }
            by_service = {
                r["service"]: r["n"]
                for r in conn.execute(
                    "SELECT COALESCE(service, '?') AS service, COUNT(*) AS n "
                    "FROM operations GROUP BY service"
                )
            }
            failed_recent = [
                self._decode(dict(r))
                for r in conn.execute(
                    "SELECT * FROM operations WHERE status = 'failed' "
                    "ORDER BY finished_at DESC LIMIT 10"
                )
            ]
            totals = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(dead_letter) AS dead_letters, "
                "SUM(attempt_count) AS attempts FROM operations"
            ).fetchone()
        return {
            "total_operations": totals["total"] or 0,
            "total_attempts": totals["attempts"] or 0,
            "dead_letters": totals["dead_letters"] or 0,
            "by_status": by_status,
            "by_service": by_service,
            "recent_failures": failed_recent,
            "generated_at": _now_iso(),
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("payload", "result"):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        row["dead_letter"] = bool(row.get("dead_letter"))
        return row
