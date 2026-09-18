"""Interface de ferramentas (tools).

Toda capacidade externa (Drive, Sheets, Telegram, LLM, redes sociais)
será implementada como uma BaseTool nas fases seguintes. Os agentes
dependem desta interface — nunca de implementações concretas — o que
permite mocks em teste e modo dry-run em dev.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Resultado padronizado de qualquer tool."""

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    retryable: bool = False


class BaseTool(ABC):
    """Contrato mínimo de uma tool do sistema."""

    name: str
    description: str = ""

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """Executa a tool. Implementações devem ser idempotentes
        sempre que possível (ver estratégia de idempotência da FASE 0)."""
        raise NotImplementedError

    def __call__(self, **kwargs: Any) -> ToolResult:
        return self.run(**kwargs)


class ToolRegistry:
    """Registro central de tools, resolvido por nome.

    Permite trocar implementações por ambiente (real em prod,
    mock/dry-run em dev/testes) sem alterar os agentes.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool não registrada: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)


# Instância global usada pela aplicação; testes criam a própria.
tool_registry = ToolRegistry()
