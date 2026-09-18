"""Classe base dos agentes.

Cada agente é um nó do grafo: recebe o AgentState, executa sua
responsabilidade e devolve um dict de atualizações de estado
(padrão LangGraph). A lógica real (LLM, tools) chega nas FASES 2+.
"""

from abc import ABC, abstractmethod
from typing import Any

from config import get_logger
from schemas import AgentEvent, AgentName, AgentState
from tools import ToolRegistry, tool_registry


class BaseAgent(ABC):
    name: AgentName

    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools or tool_registry
        self.logger = get_logger(f"agent.{self.name.value}")
        self.events: list[AgentEvent] = []  # coletor simples (fase fundação)

    def __call__(self, state: AgentState) -> dict[str, Any]:
        self.logger.info(
            "agente invocado",
            extra={
                "run_id": state.run_id,
                "product_id": state.product_id,
                "agent": self.name.value,
                "stage": state.current_stage.value,
            },
        )
        return self.run(state)

    @abstractmethod
    def run(self, state: AgentState) -> dict[str, Any]:
        """Retorna atualizações parciais para o estado do grafo."""
        raise NotImplementedError
