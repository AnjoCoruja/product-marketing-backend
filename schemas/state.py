"""Estado global do grafo LangGraph (AgentState).

Este é o estado que trafega entre os nós do StateGraph e é
persistido pelo checkpointer. Payloads grandes (imagens, copy)
NÃO ficam aqui — apenas referências (IDs do Drive/Sheets).
"""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from .enums import AgentName, RunType, Stage, TriggerSource


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StageAttempts(BaseModel):
    """Contador de tentativas por estágio (suporta a política de retries)."""

    attempts: dict[str, int] = Field(default_factory=dict)

    def increment(self, stage: Stage) -> int:
        self.attempts[stage.value] = self.attempts.get(stage.value, 0) + 1
        return self.attempts[stage.value]

    def get(self, stage: Stage) -> int:
        return self.attempts.get(stage.value, 0)


class AgentState(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    run_type: RunType = RunType.NEW_PRODUCT
    trigger_source: TriggerSource = TriggerSource.TELEGRAM

    # Referências de domínio (nunca payloads)
    product_id: str | None = None
    campaign_id: str | None = None

    # Máquina de estados
    current_stage: Stage = Stage.INTAKE
    active_agent: AgentName | None = None

    # Human-in-the-loop
    pending_human_action: str | None = None  # ex.: "campaign_approval"
    human_decision: str | None = None        # approved | rejected
    human_feedback: str | None = None

    # Entrada bruta (intake) — pequena, ok no estado
    intake_text: str = ""
    intake_photo_file_ids: list[str] = Field(default_factory=list)
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None

    # Controle operacional
    attempts: StageAttempts = Field(default_factory=StageAttempts)
    errors: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
