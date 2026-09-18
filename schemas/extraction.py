"""Schema de extração do Product Agent (saída estruturada do LLM).

Contrato exato do retorno ao n8n/Telegram: campos nulos quando a
informação não está disponível e `missing_fields` listando o que falta.
"""

from pydantic import BaseModel, Field


PRODUCT_FIELDS = ["name", "description", "color", "size", "wholesale_price", "retail_price"]


class ProductExtraction(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    size: str | None = None
    wholesale_price: float | None = None
    retail_price: float | None = None
    missing_fields: list[str] = Field(default_factory=list)

    def compute_missing(self) -> list[str]:
        """Recalcula missing_fields a partir dos campos nulos/vazios.

        Fonte de verdade é o dado, não o que o LLM declarou —
        protege contra alucinação do campo missing_fields.
        """
        missing = []
        for field in PRODUCT_FIELDS:
            value = getattr(self, field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)
        return missing

    def with_computed_missing(self) -> "ProductExtraction":
        self.missing_fields = self.compute_missing()
        return self
