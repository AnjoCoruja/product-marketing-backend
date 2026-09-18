"""Product Agent — FASE 2.

Fluxo: texto e/ou foto (base64) -> analyze_product_image (LLM multimodal)
-> validate_product -> resposta estruturada para o n8n.

O agente expõe `process()` (uso direto pelo endpoint HTTP) e `run()`
(nó do LangGraph, mantendo compatibilidade com o grafo da FASE 1).
Sem memória persistente nesta fase: cada chamada é independente.
"""

from datetime import datetime, timezone
from typing import Any

from schemas import AgentName, AgentState, Stage
from schemas.extraction import ProductExtraction
from tools import AnalyzeProductImageTool, ToolRegistry, ValidateProductTool, tool_registry

from .base import BaseAgent


class ProductAgent(BaseAgent):
    name = AgentName.PRODUCT

    def __init__(
        self,
        tools: ToolRegistry | None = None,
        analyze_tool: AnalyzeProductImageTool | None = None,
        validate_tool: ValidateProductTool | None = None,
    ) -> None:
        super().__init__(tools)
        registry = tools or tool_registry
        self.analyze = analyze_tool or AnalyzeProductImageTool()
        self.validate = validate_tool or ValidateProductTool()
        registry.register(self.analyze)
        registry.register(self.validate)

    def process(self, text: str = "", image_base64: str | None = None) -> dict[str, Any]:
        """Entrada principal usada pelo endpoint /product/analyze.

        Retorna o JSON estruturado que o n8n encaminha ao Telegram.
        """
        analysis = self.analyze.run(text=text, image_base64=image_base64)
        if not analysis.success:
            self.logger.warning("analyze_product_image falhou: %s", analysis.error)
            return {
                "success": False,
                "error": analysis.error,
                "retryable": analysis.retryable,
            }

        validation = self.validate.run(extraction=analysis.data)
        if not validation.success:
            self.logger.warning("validate_product falhou: %s", validation.error)
            return {"success": False, "error": validation.error, "retryable": False}

        extraction = ProductExtraction(**validation.data["extraction"])
        result = {
            "success": True,
            "product_id": validation.data["product_id"],
            "is_complete": validation.data["is_complete"],
            "missing_fields": validation.data["missing_fields"],
            **extraction.model_dump(exclude={"missing_fields"}),
        }
        self.logger.info(
            "produto extraído",
            extra={
                "product_id": result["product_id"],
                "agent": self.name.value,
            },
        )
        return result

    # --- Nó do LangGraph (compatível com o grafo da FASE 1) ---
    def run(self, state: AgentState) -> dict[str, Any]:
        result = self.process(text=state.intake_text)
        if not result["success"]:
            return {
                "current_stage": Stage.FAILED,
                "errors": state.errors + [result["error"]],
                "updated_at": datetime.now(timezone.utc),
            }
        return {
            "product_id": result["product_id"],
            "current_stage": Stage.CATALOGING,
            "active_agent": self.name,
            "updated_at": datetime.now(timezone.utc),
        }
