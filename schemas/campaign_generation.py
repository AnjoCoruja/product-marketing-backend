"""Schema de geração do Marketing Agent (saída estruturada do LLM).

Espelha o ProductExtraction: o LLM preenche estes campos e a
validação determinística (CampaignValidation) verifica as regras
anti-alucinação antes de virar Campaign de domínio.
"""

from pydantic import BaseModel, Field


class CampaignGeneration(BaseModel):
    """Conteúdo bruto da campanha gerado pelo LLM."""

    headline: str = ""
    marketing_concept: str = ""
    caption_instagram: str = ""
    caption_facebook: str = ""
    caption_tiktok: str = ""
    hashtags: list[str] = Field(default_factory=list)
    image_prompt: str = ""
    cta: str = ""
