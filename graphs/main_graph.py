"""Grafo principal (StateGraph) — esqueleto da fundação.

Nós ativos nesta fase:
    router -> product_agent -> marketing_agent -> approval -> finish

Pontos de expansão já marcados para:
    - Image Generation Agent (entre marketing e approval)
    - Social Media Agent (após aprovação)
    - Video / Analytics Agents (subgrafos futuros)
"""

from datetime import datetime, timezone
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents import MarketingAgent, ProductAgent
from schemas import AgentState, RunType, Stage, TriggerSource

from .routers import route_after_approval, route_by_run_type


def finish_node(state: AgentState) -> dict:
    return {
        "current_stage": Stage.DONE,
        "active_agent": None,
        "pending_human_action": None,
        "updated_at": datetime.now(timezone.utc),
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop stub.

    Em produção este ponto será um interrupt do LangGraph: a run pausa,
    o n8n envia a prévia no Telegram e o resume chega com a decisão.
    Na fundação, `human_decision` já presente no estado é respeitada;
    ausente, a run segue para finish sem publicar nada.
    """
    return {"updated_at": datetime.now(timezone.utc)}


def build_graph(checkpointer=None):
    """Monta e compila o StateGraph.

    Checkpointer injetável: MemorySaver em dev/testes,
    PostgresSaver em produção (FASE futura).
    """
    product_agent = ProductAgent()
    marketing_agent = MarketingAgent()

    graph = StateGraph(AgentState)

    graph.add_node("product_agent", product_agent)
    graph.add_node("marketing_agent", marketing_agent)
    graph.add_node("approval", approval_node)
    graph.add_node("finish", finish_node)

    graph.set_conditional_entry_point(
        route_by_run_type,
        {"product_agent": "product_agent", "marketing_agent": "marketing_agent"},
    )
    graph.add_edge("product_agent", "marketing_agent")
    graph.add_edge("marketing_agent", "approval")
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"marketing_agent": "marketing_agent", "finish": "finish"},
    )
    graph.add_edge("finish", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


def create_initial_state(
    run_type: RunType = RunType.NEW_PRODUCT,
    trigger_source: TriggerSource = TriggerSource.TELEGRAM,
    intake_text: str = "",
    intake_photo_file_ids: list[str] | None = None,
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
    product_id: str | None = None,
) -> AgentState:
    """Fábrica de estado inicial a partir de um gatilho externo."""
    return AgentState(
        run_id=str(uuid4()),
        run_type=run_type,
        trigger_source=trigger_source,
        intake_text=intake_text,
        intake_photo_file_ids=intake_photo_file_ids or [],
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
        product_id=product_id,
    )
