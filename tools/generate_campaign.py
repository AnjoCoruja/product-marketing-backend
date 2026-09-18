"""Tool: generate_campaign

Chama o LLM com o Product JSON (saída do Product Agent) e devolve um
CampaignGeneration validado. Mesma estratégia do analyze_product_image:
modelo injetável para testes e retry corretivo com temperatura reduzida.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import get_logger, get_settings
from schemas.campaign_generation import CampaignGeneration

from .base import BaseTool, ToolResult

logger = get_logger("tool.generate_campaign")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "campaign_generation.txt"


class GenerateCampaignTool(BaseTool):
    name = "generate_campaign"
    description = (
        "Gera conceito, headline, copy por plataforma, CTA, hashtags e "
        "image_prompt de uma campanha a partir do JSON do produto."
    )

    def __init__(self, chat_model: Any = None) -> None:
        self._chat_model = chat_model

    def _get_model(self):
        if self._chat_model is None:
            from langchain.chat_models import init_chat_model

            settings = get_settings()
            if not settings.llm_api_key:
                raise RuntimeError("LLM_API_KEY não configurada no .env")
            self._chat_model = init_chat_model(
                settings.llm_model,
                model_provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                temperature=settings.llm_temperature,
            )
        return self._chat_model

    def _build_message(self, product: dict[str, Any]):
        import json

        from langchain_core.messages import HumanMessage, SystemMessage

        system = SystemMessage(content=PROMPT_PATH.read_text(encoding="utf-8"))
        human = HumanMessage(
            content=f"JSON do produto:\n{json.dumps(product, ensure_ascii=False, indent=2)}"
        )
        return [system, human]

    @retry(
        retry=retry_if_exception_type((ValidationError, ValueError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _generate(self, product: dict[str, Any]) -> CampaignGeneration:
        model = self._get_model()
        structured = model.with_structured_output(CampaignGeneration)
        result: CampaignGeneration = structured.invoke(self._build_message(product))
        if not isinstance(result, CampaignGeneration):
            raise ValueError(f"LLM retornou tipo inesperado: {type(result)}")
        return result

    def run(self, product: dict[str, Any] | None = None, **_: Any) -> ToolResult:
        if not product or not product.get("name"):
            return ToolResult(
                success=False,
                error="generate_campaign requer o JSON do produto com pelo menos 'name'.",
                retryable=False,
            )
        try:
            generation = self._generate(product)
            return ToolResult(success=True, data=generation.model_dump())
        except Exception as exc:  # noqa: BLE001
            logger.warning("falha na geração de campanha: %s", exc)
            return ToolResult(success=False, error=str(exc), retryable=True)
