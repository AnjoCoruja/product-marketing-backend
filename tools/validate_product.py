"""Tool: validate_product

Valida um ProductExtraction (ou dict equivalente) e monta o Product
de domínio. Não chama LLM — é validação determinística:
- recalcula missing_fields;
- normaliza preços (negativos viram erro de negócio);
- gera o product_id (UUID) usado na resposta ao n8n.
"""

from typing import Any

from schemas import Product
from schemas.extraction import PRODUCT_FIELDS, ProductExtraction

from .base import BaseTool, ToolResult


class ValidateProductTool(BaseTool):
    name = "validate_product"
    description = (
        "Valida a extração do produto, recalcula missing_fields e "
        "gera o Product de domínio com product_id."
    )

    def run(self, extraction: dict[str, Any] | ProductExtraction, **_: Any) -> ToolResult:
        try:
            if isinstance(extraction, dict):
                extraction = ProductExtraction(**extraction)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"Extração inválida: {exc}",
                retryable=False,
            )

        for price_field in ("wholesale_price", "retail_price"):
            value = getattr(extraction, price_field)
            if value is not None and value < 0:
                return ToolResult(
                    success=False,
                    error=f"{price_field} não pode ser negativo: {value}",
                    retryable=False,
                )

        extraction = extraction.with_computed_missing()

        product = Product(
            name=extraction.name or "",
            description=extraction.description or "",
            price=extraction.retail_price,
            attributes={
                k: v
                for k, v in {
                    "color": extraction.color,
                    "size": extraction.size,
                    "wholesale_price": (
                        str(extraction.wholesale_price)
                        if extraction.wholesale_price is not None
                        else None
                    ),
                }.items()
                if v is not None
            },
        )

        return ToolResult(
            success=True,
            data={
                "product_id": product.product_id,
                "extraction": extraction.model_dump(),
                "is_complete": not extraction.missing_fields,
                "missing_fields": list(extraction.missing_fields),
                "required_fields": PRODUCT_FIELDS,
            },
        )
