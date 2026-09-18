"""Tool: validate_campaign

Validação determinística (sem LLM) do CampaignGeneration contra o
Product JSON — é a barreira anti-alucinação do Marketing Agent:

- preços: qualquer valor monetário citado nos textos deve ser
  exatamente o retail_price do produto (preço nunca é alterado);
- estoque: termos de disponibilidade/quantidade são proibidos;
- tamanhos e cores: só pode mencionar os informados no produto;
- campos obrigatórios da campanha não podem estar vazios.
"""

import re
from typing import Any

from schemas import Campaign, CampaignCopy, CampaignStatus
from schemas.campaign_generation import CampaignGeneration

from .base import BaseTool, ToolResult

TEXT_FIELDS = (
    "headline",
    "marketing_concept",
    "caption_instagram",
    "caption_facebook",
    "caption_tiktok",
    "cta",
)

REQUIRED_FIELDS = TEXT_FIELDS + ("hashtags", "image_prompt")

STOCK_TERMS = re.compile(
    r"\b(estoque|dispon[ií]vel|disponibilidade|pronta\s*entrega|"
    r"unidades?|restam|últimas|acabando|limitad[oa]s?)\b",
    re.IGNORECASE,
)

MONEY_PATTERN = re.compile(
    r"(?:R\$\s*(\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?))|"
    r"((?:\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?))\s*reais\b",
    re.IGNORECASE,
)


def _parse_brl(value: str) -> float | None:
    value = value.strip()
    try:
        if "," in value:
            return float(value.replace(".", "").replace(",", "."))
        return float(value)
    except ValueError:
        return None


class ValidateCampaignTool(BaseTool):
    name = "validate_campaign"
    description = (
        "Valida o conteúdo gerado contra o JSON do produto (anti-alucinação) "
        "e monta a Campaign de domínio."
    )

    def _texts(self, gen: CampaignGeneration) -> dict[str, str]:
        return {f: getattr(gen, f) for f in TEXT_FIELDS}

    def _check_prices(self, texts: dict[str, str], retail_price: float | None) -> str | None:
        for field, text in texts.items():
            for match in MONEY_PATTERN.finditer(text or ""):
                raw = match.group(1) or match.group(2)
                value = _parse_brl(raw)
                if value is None:
                    continue
                if retail_price is None:
                    return f"{field}: menciona preço (R$ {raw}) mas o produto não tem retail_price."
                if abs(value - retail_price) > 0.001:
                    return (
                        f"{field}: preço alterado — citou R$ {value:.2f}, "
                        f"mas retail_price é R$ {retail_price:.2f}."
                    )
        return None

    def _check_stock(self, texts: dict[str, str]) -> str | None:
        for field, text in texts.items():
            found = STOCK_TERMS.search(text or "")
            if found:
                return f"{field}: termo proibido de estoque/disponibilidade: '{found.group(0)}'."
        return None

    def _check_sizes(self, gen: CampaignGeneration, size: str | None) -> str | None:
        pattern = re.compile(r"\btamanho[:\s]+([A-Za-z0-9]{1,4})\b", re.IGNORECASE)
        allowed = (size or "").strip().upper()
        for field in ("caption_instagram", "caption_facebook", "caption_tiktok", "headline"):
            text = getattr(gen, field) or ""
            mentions = pattern.findall(text)
            if mentions and not allowed:
                return f"{field}: menciona tamanho, mas o produto não informa size."
            for mentioned in mentions:
                if allowed and mentioned.upper() != allowed:
                    return (
                        f"{field}: tamanho inventado '{mentioned}' — "
                        f"o produto informa apenas '{size}'."
                    )
        return None

    def run(
        self,
        generation: dict[str, Any] | CampaignGeneration | None = None,
        product: dict[str, Any] | None = None,
        **_: Any,
    ) -> ToolResult:
        if not product or not product.get("product_id"):
            return ToolResult(
                success=False,
                error="validate_campaign requer o JSON do produto com 'product_id'.",
                retryable=False,
            )
        try:
            if isinstance(generation, dict):
                generation = CampaignGeneration(**generation)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"Geração inválida: {exc}", retryable=False)
        if generation is None:
            return ToolResult(
                success=False, error="validate_campaign requer 'generation'.", retryable=False
            )

        empty = [
            f
            for f in REQUIRED_FIELDS
            if not getattr(generation, f) or (f == "hashtags" and not generation.hashtags)
        ]
        if empty:
            return ToolResult(
                success=False,
                error=f"Campos obrigatórios da campanha vazios: {', '.join(empty)}",
                retryable=True,
            )

        texts = self._texts(generation)
        for error in (
            self._check_prices(texts, product.get("retail_price")),
            self._check_stock(texts),
            self._check_sizes(generation, product.get("size")),
        ):
            if error:
                return ToolResult(
                    success=False,
                    error=f"Violação anti-alucinação: {error}",
                    retryable=True,
                )

        campaign = Campaign(
            product_id=product["product_id"],
            concept=generation.marketing_concept,
            ad_copy=CampaignCopy(
                instagram=generation.caption_instagram,
                facebook=generation.caption_facebook,
                tiktok=generation.caption_tiktok,
                hashtags=[h if h.startswith("#") else f"#{h}" for h in generation.hashtags],
                cta=generation.cta,
            ),
            status=CampaignStatus.DRAFT,
        )

        return ToolResult(
            success=True,
            data={
                "campaign": campaign.model_dump(mode="json", by_alias=True),
                "headline": generation.headline,
                "image_prompt": generation.image_prompt,
            },
        )
