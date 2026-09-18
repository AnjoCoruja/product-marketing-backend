"""Tool: analyze_product_image

Chama o LLM multimodal com a foto (base64) e/ou texto e devolve
um ProductExtraction validado. Erros de parse viram retry corretivo
(1x) com temperatura reduzida, conforme a estratégia de retries.
"""

import base64
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
from schemas.extraction import ProductExtraction

from .base import BaseTool, ToolResult

logger = get_logger("tool.analyze_product_image")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "product_extraction.txt"


class AnalyzeProductImageTool(BaseTool):
    name = "analyze_product_image"
    description = (
        "Analisa foto (base64) e/ou texto de um produto com LLM multimodal "
        "e retorna os campos estruturados + missing_fields."
    )

    def __init__(self, chat_model: Any = None) -> None:
        # Injetável para testes (fake chat model). Em produção, criado
        # a partir das settings na primeira chamada.
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

    def _build_message(self, text: str, image_base64: str | None):
        from langchain_core.messages import HumanMessage, SystemMessage

        system = SystemMessage(content=PROMPT_PATH.read_text(encoding="utf-8"))
        content: list[dict] = [
            {"type": "text", "text": f"Mensagem do usuário: {text or '(sem texto)'}"}
        ]
        if image_base64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                }
            )
        return [system, HumanMessage(content=content)]

    @retry(
        retry=retry_if_exception_type((ValidationError, ValueError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _extract(self, text: str, image_base64: str | None) -> ProductExtraction:
        model = self._get_model()
        structured = model.with_structured_output(ProductExtraction)
        result: ProductExtraction = structured.invoke(self._build_message(text, image_base64))
        if not isinstance(result, ProductExtraction):
            raise ValueError(f"LLM retornou tipo inesperado: {type(result)}")
        # missing_fields é recalculado a partir dos dados (anti-alucinação)
        return result.with_computed_missing()

    def run(
        self,
        text: str = "",
        image_base64: str | None = None,
        **_: Any,
    ) -> ToolResult:
        if not text and not image_base64:
            return ToolResult(
                success=False,
                error="analyze_product_image requer texto e/ou imagem.",
                retryable=False,
            )
        try:
            extraction = self._extract(text, image_base64)
            return ToolResult(success=True, data=extraction.model_dump())
        except Exception as exc:  # noqa: BLE001 — erro vira ToolResult padronizado
            logger.warning("falha na extração: %s", exc)
            return ToolResult(success=False, error=str(exc), retryable=True)


def image_file_to_base64(path: str) -> str:
    """Helper: converte arquivo local de imagem para base64."""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")
