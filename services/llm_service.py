"""Camada LangChain — estrutura básica (FASE 1).

Centraliza a criação de modelos de chat conforme o provider
configurado. Nenhuma chamada real de LLM acontece nesta fase;
o factory só é ativado quando uma API key estiver configurada.
"""

from config import get_logger, get_settings

logger = get_logger("services.llm")


class LLMService:
    """Factory de chat models LangChain por provider.

    FASE 2: adicionar init_chat_model("gpt-4o", ...), modelo
    multimodal para o Product Agent e output parsers Pydantic.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_api_key)

    def get_chat_model(self, temperature: float | None = None):
        """Retorna um chat model LangChain.

        Levanta erro claro se a FASE 1 tentar usar sem credencial —
        comportamento intencional: nunca falhar silenciosamente.
        """
        if not self.configured:
            raise RuntimeError(
                "LLM não configurado: defina LLM_API_KEY no .env "
                "(necessário apenas a partir da FASE 2)."
            )
        # FASE 2: from langchain.chat_models import init_chat_model
        # return init_chat_model(
        #     self.settings.llm_model,
        #     model_provider=self.settings.llm_provider,
        #     temperature=temperature ?? self.settings.llm_temperature,
        # )
        raise NotImplementedError("Factory de LLM chega na FASE 2.")
