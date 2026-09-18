"""Arestas condicionais do grafo principal.

FASE 0/1: roteamento mínimo (tipo de run + decisão humana).
A rota do Image Generation Agent e o circuit breaker por
plataforma entram quando esses agentes existirem.
"""

from schemas import AgentState, RunType


def route_by_run_type(state: AgentState) -> str:
    """new_product passa pelo Product Agent; recampaign vai direto
    ao Marketing Agent (o produto já existe)."""
    if state.run_type == RunType.NEW_PRODUCT:
        return "product_agent"
    return "marketing_agent"


def route_after_approval(state: AgentState) -> str:
    """Decisão humana sobre a campanha."""
    if state.human_decision == "approved":
        return "finish"  # FASE futura: "social_agent"
    if state.human_decision == "rejected":
        return "marketing_agent"  # regenera com feedback
    return "finish"
