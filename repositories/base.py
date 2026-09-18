"""Interfaces (Protocols) da camada Repository.

Agentes, services e API dependem DESTAS interfaces — nunca do
PostgreSQL/SQLite diretamente. Qualquer implementação que satisfaça
o Protocol pode ser injetada (PostgreSQL em produção, os registries
SQLite legados em dev/testes, fakes em memória em testes unitários).

Assinaturas espelham exatamente os métodos que o sistema já usa nos
registries, para que a troca de backend não altere a lógica dos agentes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProductRepository(Protocol):
    """Produtos: ids sequenciais humanos, idempotência de intake."""

    def next_product_id(self) -> str: ...

    def find_by_message(
        self, telegram_chat_id: int, message_id: int
    ) -> dict[str, Any] | None: ...

    def create(
        self,
        *,
        product_id: str,
        telegram_chat_id: int | None = None,
        message_id: int | None = None,
        name: str | None = None,
    ) -> None: ...

    def update(self, product_id: str, **fields: Any) -> None: ...

    def get(self, product_id: str) -> dict[str, Any] | None: ...

    def latest(self) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


@runtime_checkable
class CampaignRepository(Protocol):
    """Campanhas, imagens de marketing, aprovações e rotina diária."""

    def next_campaign_id(self) -> str: ...

    def create(
        self,
        campaign_id: str,
        product_id: str,
        fingerprint: str | None = None,
        status: str = "draft",
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get(self, campaign_id: str) -> dict[str, Any] | None: ...

    def update_status(self, campaign_id: str, status: str) -> None: ...

    def update_fingerprint(self, campaign_id: str, fingerprint: str) -> None: ...

    def update_content(self, campaign_id: str, content: dict[str, Any]) -> None: ...

    def get_content(self, campaign_id: str) -> dict[str, Any] | None: ...

    def latest_for_product(self, product_id: str) -> dict[str, Any] | None: ...

    def recent_campaigns(
        self, product_id: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...

    # Aprovação humana (AUTO_PUBLISH=false)
    def create_approval(
        self,
        campaign_id: str,
        telegram_chat_id: int,
        decision_options: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def get_pending_approval(
        self, chat_id: int | None = None, campaign_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def resolve_approval(
        self, approval_id: int, status: str, decision: str | None = None
    ) -> dict[str, Any]: ...

    # Imagens de marketing
    def next_image_seq(self, campaign_id: str) -> int: ...

    def register_image(
        self,
        campaign_id: str,
        image_seq: int,
        file_name: str,
        drive_file_id: str | None = None,
        drive_url: str | None = None,
        status: str = "generated",
    ) -> dict[str, Any]: ...

    def get_image(
        self, campaign_id: str, image_seq: int
    ) -> dict[str, Any] | None: ...

    def list_images(self, campaign_id: str) -> list[dict[str, Any]]: ...

    # Rotina diária (recampaign 08:00)
    def start_daily_run(
        self, run_date: str, *, reason: str, product_id: str | None = None
    ) -> bool: ...

    def finish_daily_run(
        self,
        run_date: str,
        *,
        status: str,
        detail: str = "",
        product_id: str | None = None,
        campaign_id: str | None = None,
    ) -> None: ...

    def get_daily_run(self, run_date: str) -> dict[str, Any] | None: ...

    def list_daily_runs(self, limit: int = 30) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


@runtime_checkable
class SocialPostRepository(Protocol):
    """Publicações sociais por campanha/rede (idempotente)."""

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
    ) -> dict[str, Any]: ...

    def get(self, campaign_id: str, platform: str) -> dict[str, Any] | None: ...

    def list_by_campaign(self, campaign_id: str) -> list[dict[str, Any]]: ...

    def already_published(self, campaign_id: str, platform: str) -> bool: ...

    def close(self) -> None: ...


@runtime_checkable
class AgentStateRepository(Protocol):
    """Estado/observabilidade: operações (dead-letter) e eventos dos agentes."""

    # Operações (ciclo de vida da FASE 9)
    def create(
        self,
        *,
        kind: str,
        service: str | None = None,
        stage: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]: ...

    def get(self, operation_id: str) -> dict[str, Any] | None: ...

    def find(self, ref: str) -> dict[str, Any] | None: ...

    def transition(
        self, operation_id: str, to_status: Any, **fields: Any
    ) -> dict[str, Any]: ...

    def start(self, operation_id: str, **fields: Any) -> dict[str, Any]: ...

    def fail(
        self,
        operation_id: str,
        *,
        error: str,
        error_type: str | None = None,
        service: str | None = None,
        stage: str | None = None,
        dead_letter: bool | None = None,
    ) -> dict[str, Any]: ...

    def retry(
        self, ref: str, from_stage: str | None = None
    ) -> dict[str, Any]: ...

    def list_dead_letter(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def summary(self) -> dict[str, Any]: ...

    def close(self) -> None: ...

    # Eventos dos agentes (FASE 1 — append-only)
    def record_agent_event(self, event: Any) -> dict[str, Any]: ...

    def list_agent_events(
        self,
        *,
        run_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class LogRepository(Protocol):
    """Log estruturado persistido (auditoria além do stdout)."""

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
    ) -> dict[str, Any]: ...

    def list(
        self,
        *,
        correlation_id: str | None = None,
        product_id: str | None = None,
        campaign_id: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...
