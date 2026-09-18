"""Contratos de eventos entre agentes e eventos de erro."""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from .enums import AgentName, ErrorCategory, EventType


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentEvent(BaseModel):
    """Evento de transição entre agentes/estágios.

    Payloads grandes trafegam por referência (payload_ref),
    nunca embutidos no evento.
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: EventType
    run_id: str
    product_id: str | None = None
    campaign_id: str | None = None
    from_agent: AgentName | None = None
    to_agent: AgentName | None = None
    payload_ref: str | None = None  # ex.: "sheets:products!A2", "drive:fileId"
    detail: str = ""
    timestamp: datetime = Field(default_factory=_now)


class ErrorEvent(BaseModel):
    """Erro classificado segundo a estratégia de erros da FASE 0."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    product_id: str | None = None
    agent: AgentName | None = None
    stage: str = ""
    category: ErrorCategory
    message: str
    exception_type: str | None = None
    retryable: bool = False
    attempt: int = 0
    timestamp: datetime = Field(default_factory=_now)
