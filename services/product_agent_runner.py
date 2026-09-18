"""Runner do Product Agent stateful (FASE 3).

Encapsula o grafo LangGraph com checkpointer persistente. Cada produto
roda em uma thread própria (`thread_id == product_id`), então:

- `start()` inicia uma nova thread e roda até completar ou interromper
  (pergunta ao usuário);
- `resume()` continua a thread existente com a resposta do usuário —
  nenhum agente/estado novo é criado;
- `get_state()` expõe o checkpoint atual (o n8n usa para decidir se a
  mensagem recebida é resposta de uma pergunta pendente).

Persistência: `langgraph-checkpoint-sqlite` (SqliteSaver) por padrão.
Por quê SQLite e não Redis/Postgres nesta fase:
- o estado precisa sobreviver a restart do processo (WAIT pode durar
  dias) — MemorySaver não serve para produção;
- SQLite é transacional, embarcado (zero infra extra) e gravado em
  disco — suficiente para o volume de um único operador no Telegram;
- a interface BaseCheckpointSaver é a mesma do PostgresSaver: trocar
  depois é uma linha (settings.database_url) sem tocar no grafo.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from graphs.product_agent_graph import build_product_agent_graph
from schemas.extraction import PRODUCT_FIELDS
from schemas.product_state import ProductAgentState
from services.product_registry import ProductRegistry
from tools import (
    AnalyzeProductImageTool,
    CreateProductRecordTool,
    SaveProductImageTool,
    ValidateProductTool,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductAgentRunner:
    def __init__(
        self,
        checkpointer: Any = None,
        db_path: str | Path | None = None,
        analyze_tool: AnalyzeProductImageTool | None = None,
        validate_tool: ValidateProductTool | None = None,
        save_image_tool: SaveProductImageTool | None = None,
        create_record_tool: CreateProductRecordTool | None = None,
        registry: ProductRegistry | None = None,
    ) -> None:
        self._owns_connection = False
        if checkpointer is not None:
            saver = checkpointer
        else:
            path = Path(db_path or "data/product_agent_checkpoints.db")
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            saver = SqliteSaver(conn)
            self._owns_connection = True
        self.checkpointer = saver
        self.registry = registry or ProductRegistry()
        self.graph = build_product_agent_graph(
            analyze_tool=analyze_tool,
            validate_tool=validate_tool,
            save_image_tool=save_image_tool,
            create_record_tool=create_record_tool,
            checkpointer=saver,
        )

    @staticmethod
    def _config(product_id: str) -> dict:
        return {"configurable": {"thread_id": product_id}}

    def start(
        self,
        *,
        user_id: str,
        telegram_chat_id: int,
        message_id: int | None = None,
        intake_text: str = "",
        image_base64: str | None = None,
        original_image: str | None = None,
        product_id: str | None = None,
    ) -> dict[str, Any]:
        """Inicia o fluxo para um novo produto.

        Retorna o resultado parcial: `status == "complete"` (produto
        completo) ou `"awaiting_user"` com a pergunta a enviar no
        Telegram.
        """
        pid = product_id or self.registry.next_product_id()
        state: ProductAgentState = {
            "user_id": user_id,
            "telegram_chat_id": telegram_chat_id,
            "message_id": message_id,
            "product_id": pid,
            "original_image": original_image,
            "intake_text": intake_text,
            "missing_fields": [],
            "parse_errors": [],
            **{f: None for f in PRODUCT_FIELDS},
            "current_node": "receive_product",
            "status": "received",
            "created_at": _now(),
            "updated_at": _now(),
        }
        if image_base64:
            state["_image_base64"] = image_base64

        # Idempotência de intake: a mesma mensagem do Telegram não cria
        # um segundo produto.
        self.registry.create(
            product_id=pid,
            telegram_chat_id=telegram_chat_id,
            message_id=message_id,
        )

        self.graph.invoke(state, config=self._config(pid))
        snapshot = self._snapshot(pid)
        if snapshot.get("status") == "complete":
            self.registry.update(
                pid,
                name=snapshot.get("name"),
                drive_url=snapshot.get("drive_url"),
                sheet_row=snapshot.get("sheet_row"),
                status="active",
            )
        return snapshot

    def resume(self, product_id: str, user_message: str) -> dict[str, Any]:
        """Retoma a thread do produto com a resposta do usuário.

        Só faz sentido quando a thread está interrompida aguardando
        resposta (status awaiting_user). Fora disso, levanta ValueError
        em vez de criar uma run estranha na thread.
        """
        state = self.get_state(product_id)
        if state is None:
            raise ValueError(f"Produto não encontrado: {product_id}")
        if state.get("status") != "awaiting_user":
            raise ValueError(
                f"Produto {product_id} não está aguardando resposta "
                f"(status={state.get('status')})."
            )
        self.graph.invoke(
            Command(resume=user_message), config=self._config(product_id)
        )
        snapshot = self._snapshot(product_id)
        if snapshot.get("status") == "complete":
            self.registry.update(
                product_id,
                name=snapshot.get("name"),
                drive_url=snapshot.get("drive_url"),
                sheet_row=snapshot.get("sheet_row"),
                status="active",
            )
        return snapshot

    def get_state(self, product_id: str) -> ProductAgentState | None:
        snapshot = self.graph.get_state(self._config(product_id))
        if not snapshot or not snapshot.values:
            return None
        return snapshot.values  # type: ignore[return-value]

    def is_awaiting_user(self, product_id: str) -> bool:
        state = self.get_state(product_id)
        return bool(state and state.get("status") == "awaiting_user")

    def find_by_message(
        self, telegram_chat_id: int, message_id: int
    ) -> str | None:
        """product_id já criado para esta mensagem do Telegram (dedup)."""
        row = self.registry.find_by_message(telegram_chat_id, message_id)
        return row["product_id"] if row else None

    def find_pending_product(self, telegram_chat_id: int) -> str | None:
        """Produto aguardando resposta neste chat (o mais recente)."""
        for tup in self.checkpointer.list(None):
            values = tup.checkpoint.get("channel_values", {})
            if (
                values.get("telegram_chat_id") == telegram_chat_id
                and values.get("status") == "awaiting_user"
            ):
                return values.get("product_id")
        return None

    def _snapshot(self, product_id: str) -> dict[str, Any]:
        state = self.get_state(product_id) or {}
        result = state.get("result") or {}
        base = {
            "success": state.get("status") != "failed",
            "status": state.get("status"),
            "product_id": product_id,
            "missing_fields": state.get("missing_fields", []),
            "current_node": state.get("current_node"),
        }
        if state.get("status") == "awaiting_user":
            base["question"] = state.get("pending_question")
        if state.get("status") == "complete":
            base.update({f: state.get(f) for f in PRODUCT_FIELDS})
            base["is_complete"] = True
            base["drive_url"] = state.get("drive_url")
            base["drive_file_name"] = state.get("drive_file_name")
            base["sheet_row"] = state.get("sheet_row")
        if state.get("status") == "failed":
            base["error"] = "; ".join(state.get("parse_errors", [])) or "falha desconhecida"
        return {**result, **base} if state.get("status") != "complete" else base
