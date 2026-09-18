"""Factory da camada Repository.

Decide o backend de persistência pelo ambiente (sem tocar nos agentes):

    DATABASE_BACKEND=postgres + DATABASE_URL=postgresql://...
        -> implementações PostgreSQL (repositories/postgres.py)
    ausente (dev/testes)
        -> registries SQLite legados (services/*_registry.py)

Espelho opcional para Google Sheets (visualização administrativa):
    SHEETS_MIRROR_ENABLED=true + GOOGLE_SERVICE_ACCOUNT_JSON +
    GOOGLE_SHEETS_SPREADSHEET_ID
        -> ProductRepository passa a gravar também na planilha, via
           MirroredProductRepository. O PostgreSQL continua sendo a
           fonte da verdade; o Sheets é apenas leitura administrativa.

Uso:
    repos = build_repositories()
    repos.products / repos.campaigns / repos.social_posts /
    repos.agent_states / repos.logs
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from config import get_logger, get_settings

from .base import (
    AgentStateRepository,
    CampaignRepository,
    LogRepository,
    ProductRepository,
    SocialPostRepository,
)
from .migrate import run_migrations

logger = get_logger("repositories.factory")


@dataclass
class Repositories:
    """Conjunto de repositórios — o sistema inteiro consome via estas
    interfaces, sem saber qual banco está por trás."""

    products: ProductRepository
    campaigns: CampaignRepository
    social_posts: SocialPostRepository
    agent_states: AgentStateRepository
    logs: LogRepository
    backend: str = "sqlite"

    def close(self) -> None:
        for repo in (
            self.products, self.campaigns, self.social_posts,
            self.agent_states, self.logs,
        ):
            close = getattr(repo, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — fechar é best-effort
                    pass


# ---------------------------------------------------------------------------
# Adaptadores: fazem os registries SQLite legados satisfazerem as interfaces
# ---------------------------------------------------------------------------


class LegacyAgentStateAdapter:
    """Adapta OperationRegistry (SQLite) à interface AgentStateRepository,
    acrescentando o repositório de eventos (que o SQLite não tinha)."""

    def __init__(self, registry: Any, events: Any) -> None:
        self._registry = registry
        self._events = events

    def __getattr__(self, name: str) -> Any:
        # eventos primeiro (record_agent_event, list_agent_events vivem aqui)
        if name in ("record_agent_event", "list_agent_events"):
            return getattr(self._events, name)
        return getattr(self._registry, name)

    def transition(
        self, operation_id: str, to_status: Any, **fields: Any
    ) -> dict[str, Any]:
        """Ponte pública para a máquina de estados do OperationRegistry
        (que expõe apenas _transition internamente)."""
        from services.operation_registry import OperationStatus

        target = getattr(to_status, "value", to_status)
        return self._registry._transition(
            operation_id, OperationStatus(target), updates=fields or None
        )

    def find(self, ref: str) -> dict[str, Any] | None:
        return self._registry._resolve(ref)

    def list_dead_letter(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._registry.list_dead_letters(limit)

    def close(self) -> None:
        self._events.close()


class SQLiteLogRepository:
    """LogRepository mínimo sobre SQLite (dev/testes) — mesma tabela `logs`
    do esquema PostgreSQL."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        logger TEXT NOT NULL,
        message TEXT NOT NULL,
        correlation_id TEXT,
        product_id TEXT,
        campaign_id TEXT,
        context TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_logs_correlation ON logs (correlation_id);
    CREATE INDEX IF NOT EXISTS idx_logs_product ON logs (product_id);
    """

    def __init__(self, db_path: str = "data/logs.db") -> None:
        import sqlite3
        from datetime import datetime, timezone
        from pathlib import Path

        self._now = lambda: datetime.now(timezone.utc).isoformat()
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert(
        self,
        *,
        level: str,
        logger: str,
        message: str,
        correlation_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import json

        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO logs (level, logger, message, correlation_id, "
                "product_id, campaign_id, context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    level, logger, message, correlation_id, product_id,
                    campaign_id,
                    json.dumps(context, ensure_ascii=False) if context else None,
                    self._now(),
                ),
            )
        return self._conn.execute(
            "SELECT * FROM logs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    def list(
        self,
        *,
        correlation_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if product_id:
            clauses.append("product_id = ?")
            params.append(product_id)
        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)
        if level:
            clauses.append("level = ?")
            params.append(level)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]


class SQLiteAgentEventRepository:
    """Eventos dos agentes em SQLite (dev/testes) — tabela `agent_events`."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS agent_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        run_id TEXT NOT NULL,
        product_id TEXT,
        campaign_id TEXT,
        from_agent TEXT,
        to_agent TEXT,
        payload_ref TEXT,
        detail TEXT NOT NULL DEFAULT '',
        "timestamp" TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events (run_id);
    """

    def __init__(self, db_path: str = "data/agent_events.db") -> None:
        import sqlite3
        from pathlib import Path

        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record_agent_event(self, event: Any) -> dict[str, Any]:
        from datetime import datetime, timezone
        from uuid import uuid4

        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        event_id = data.get("event_id") or str(uuid4())
        ts = data.get("timestamp") or datetime.now(timezone.utc).isoformat()
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO agent_events (event_id, event_type, "
                "run_id, product_id, campaign_id, from_agent, to_agent, "
                "payload_ref, detail, \"timestamp\") "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    getattr(data.get("event_type"), "value", data.get("event_type")),
                    data["run_id"],
                    data.get("product_id"),
                    data.get("campaign_id"),
                    getattr(data.get("from_agent"), "value", data.get("from_agent")),
                    getattr(data.get("to_agent"), "value", data.get("to_agent")),
                    data.get("payload_ref"),
                    data.get("detail", ""),
                    ts,
                ),
            )
        row = self._conn.execute(
            "SELECT * FROM agent_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row)

    def list_agent_events(
        self,
        *,
        run_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if product_id:
            clauses.append("product_id = ?")
            params.append(product_id)
        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f'SELECT * FROM agent_events {where} '
            f'ORDER BY "timestamp" DESC LIMIT ?',
            params,
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Espelho opcional: PostgreSQL (verdade) -> Google Sheets (visualização)
# ---------------------------------------------------------------------------


class PostgresSheetsBackend:
    """SheetsBackend (tools/product_sheet.py) que delega ao repositório de
    produtos: `read_all` reconstrói as linhas a partir do PostgreSQL.

    Assim as tools existentes (CreateProductRecordTool,
    UpdateProductRecordTool, GetProductTool, GetLatestProductTool)
    funcionam SEM ALTERAÇÃO — a "planilha" delas passa a ser o banco.
    Com o espelho habilitado, o Sheets real é atualizado à parte pelo
    MirroredProductRepository.
    """

    def __init__(self, products: ProductRepository) -> None:
        from tools.product_sheet import FIELD_TO_COLUMN, SHEET_COLUMNS

        self._products = products
        self._columns = SHEET_COLUMNS
        self._field_to_column = FIELD_TO_COLUMN

    def read_all(self) -> list[list[Any]]:
        # Linhas em ordem de cadastro; o header é o da planilha.
        rows: list[list[Any]] = [self._columns.copy()]
        all_products = self._all_products()
        for p in all_products:
            rows.append(self._product_to_row(p))
        return rows

    def append_row(self, values: list[Any]) -> int:
        # append é tratado pelo MirroredProductRepository (update com
        # sheet_row); aqui apenas devolvemos a posição final.
        return len(self.read_all())

    def update_row(self, row_number: int, values: list[Any]) -> None:
        product_id = values[0] if values else None
        if product_id:
            self._products.update(
                product_id, **self._row_to_updates(values)
            )

    # -- internos ------------------------------------------------------------

    def _all_products(self) -> list[dict[str, Any]]:
        latest = self._products.latest()
        if not latest:
            return []
        seq = int(latest.get("seq") or 0)
        # sem list-all na interface: reconstrói por ids sequenciais
        products = []
        for n in range(1, seq + 1):
            p = self._products.get(f"P{n:06d}")
            if p:
                products.append(p)
        return products

    def _product_to_row(self, p: dict[str, Any]) -> list[Any]:
        def col(name: str) -> Any:
            field = {
                v: k for k, v in self._field_to_column.items()
            }.get(name, name)
            return p.get(field)

        return [col(c) for c in self._columns]

    def _row_to_updates(self, values: list[Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for idx, column in enumerate(self._columns):
            if idx >= len(values):
                break
            field = {v: k for k, v in self._field_to_column.items()}.get(
                column, column
            )
            if field in {"name", "drive_file_id", "drive_url", "status"}:
                updates[field] = values[idx]
        return updates


class MirroredProductRepository:
    """ProductRepository que grava no PostgreSQL e espelha no Google Sheets
    (quando habilitado). O Sheets é eventual e NUNCA bloqueia o fluxo:
    falha no espelho vira log, não erro."""

    def __init__(self, primary: ProductRepository, sheet_tool: Any) -> None:
        self._primary = primary
        self._sheet_tool = sheet_tool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def close(self) -> None:
        self._primary.close()

    def update(self, product_id: str, **fields: Any) -> None:
        self._primary.update(product_id, **fields)
        self._mirror_update(product_id)

    def create(self, **kwargs: Any) -> None:
        self._primary.create(**kwargs)
        product_id = kwargs.get("product_id")
        if product_id:
            self._mirror_update(product_id)

    def _mirror_update(self, product_id: str) -> None:
        try:
            product = self._primary.get(product_id)
            if product and self._sheet_tool is not None:
                self._sheet_tool.run(product=product)
        except Exception as exc:  # noqa: BLE001 — espelho nunca quebra o fluxo
            logger.warning("espelho Sheets falhou para %s: %s", product_id, exc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_repositories(
    *,
    database_url: str | None = None,
    backend: str | None = None,
    sheets_mirror: bool | None = None,
) -> Repositories:
    """Constrói o conjunto de repositórios conforme o ambiente.

    Precedência: parâmetros explícitos > variáveis de ambiente/settings.
    """
    settings = get_settings()
    url = database_url or settings.database_url or os.environ.get("DATABASE_URL", "")
    backend = (
        backend
        or os.environ.get("DATABASE_BACKEND")
        or (
            getattr(settings, "database_backend", "sqlite")
            if getattr(settings, "database_backend", "sqlite") != "sqlite" or not url
            else "postgres"
        )
    )
    mirror = (
        sheets_mirror
        if sheets_mirror is not None
        else (
            settings.sheets_mirror_enabled
            or os.environ.get("SHEETS_MIRROR_ENABLED", "").lower() == "true"
        )
    )

    if backend == "postgres":
        return _build_postgres(url, mirror)
    return _build_sqlite(mirror)


def _build_postgres(database_url: str, mirror: bool) -> Repositories:
    import psycopg

    from .postgres import (
        PgAgentStateRepository,
        PgCampaignRepository,
        PgLogRepository,
        PgProductRepository,
        PgSocialPostRepository,
    )

    applied = run_migrations(database_url)
    if applied:
        logger.info("migrations aplicadas: %s", ", ".join(map(str, applied)))

    products: ProductRepository = PgProductRepository(
        psycopg.connect(database_url)
    )
    campaigns = PgCampaignRepository(psycopg.connect(database_url))
    social_posts = PgSocialPostRepository(psycopg.connect(database_url))
    agent_states = PgAgentStateRepository(psycopg.connect(database_url))
    logs = PgLogRepository(psycopg.connect(database_url))

    if mirror:
        sheet_tool = _build_sheets_mirror_tool()
        if sheet_tool is not None:
            products = MirroredProductRepository(products, sheet_tool)
            logger.info("espelho Google Sheets habilitado (admin only)")

    logger.info("repositórios PostgreSQL prontos")
    return Repositories(
        products=products,
        campaigns=campaigns,
        social_posts=social_posts,
        agent_states=agent_states,
        logs=logs,
        backend="postgres",
    )


def _build_sqlite(mirror: bool) -> Repositories:
    from services.campaign_registry import CampaignRegistry
    from services.operation_registry import OperationRegistry
    from services.product_registry import ProductRegistry
    from services.publication_registry import PublicationRegistry

    events = SQLiteAgentEventRepository()
    logs = SQLiteLogRepository()
    repos = Repositories(
        products=ProductRegistry(),
        campaigns=CampaignRegistry(),
        social_posts=PublicationRegistry(),
        agent_states=LegacyAgentStateAdapter(OperationRegistry(), events),
        logs=logs,
        backend="sqlite",
    )
    if mirror:
        sheet_tool = _build_sheets_mirror_tool()
        if sheet_tool is not None:
            repos.products = MirroredProductRepository(repos.products, sheet_tool)
    return repos


def _build_sheets_mirror_tool() -> Any | None:
    """Cria a CreateProductRecordTool com GoogleSheetsBackend real para o
    espelho administrativo. Retorna None se as credenciais não estiverem
    configuradas (espelho fica desligado silenciosamente)."""
    settings = get_settings()
    if not (
        settings.google_service_account_json
        and settings.google_sheets_spreadsheet_id
    ):
        logger.warning(
            "SHEETS_MIRROR_ENABLED=true mas credenciais Google ausentes — "
            "espelho desligado"
        )
        return None
    from tools.product_sheet import CreateProductRecordTool, GoogleSheetsBackend

    backend = GoogleSheetsBackend(
        settings.google_service_account_json,
        settings.google_sheets_spreadsheet_id,
    )
    return CreateProductRecordTool(backend)
