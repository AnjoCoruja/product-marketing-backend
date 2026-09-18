"""Tool: generate_marketing_image

Gera a imagem comercial da campanha usando a FOTO ORIGINAL do produto
como referência visual (edição guiada por imagem, não geração do zero).

O prompt final combina:
- o image_prompt criado pelo Marketing Agent;
- o conceito da campanha;
- os dados estruturados do produto (nome, cor, tamanho, descrição);
- instruções rígidas de preservação: produto, cor, formato, detalhes,
  logos e características visuais da foto original NÃO podem mudar.

Backends injetáveis (mesma filosofia Drive/Sheets):
- real: API de imagem do provider configurado (IMAGE_PROVIDER /
  IMAGE_MODEL / IMAGE_API_KEY no .env), modo image-to-image com a
  foto original em base64;
- fake (dev/testes): `FakeImageBackend` devolve um PNG mínimo válido
  (determinístico), sem nenhuma credencial.

Vídeo NÃO é gerado aqui — estrutura preparada (media_type) para
futura geração de vídeo sem mudança de contrato.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import get_logger, get_settings

from .base import BaseTool, ToolResult

logger = get_logger("tool.generate_marketing_image")

PRESERVATION_RULES = (
    "STRICT PRESERVATION RULES: use the provided reference photo as the "
    "visual ground truth. Preserve exactly: the product itself, its color, "
    "shape, format, details, logos, labels and all visual characteristics. "
    "Do not redesign, recolor or add features to the product. Only enhance "
    "the scene around it: professional commercial photography, studio "
    "lighting, clean neutral background, advertising quality."
)

# PNG 1x1 válido — placeholder determinístico do backend fake
_FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class ImageBackend(Protocol):
    """Contrato mínimo do gerador de imagem (real ou fake)."""

    def generate(
        self, *, prompt: str, reference_image_base64: str, size: str = "1024x1024"
    ) -> bytes: ...


class FakeImageBackend:
    """Gerador em memória para dev/testes — devolve PNG mínimo válido."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(
        self, *, prompt: str, reference_image_base64: str, size: str = "1024x1024"
    ) -> bytes:
        self.calls.append(
            {"prompt": prompt, "reference_image_base64": reference_image_base64, "size": size}
        )
        return _FAKE_PNG


class ApiImageBackend:
    """Backend real — API de imagem (image-to-image) do provider.

    Só importa o SDK do provider quando instanciado. Suportado nesta
    fase: OpenAI Images (gpt-image-1) em modo edição com referência.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.image_api_key:
            raise RuntimeError("IMAGE_API_KEY não configurada no .env")
        self._provider = settings.image_provider
        self._model = settings.image_model
        if self._provider == "openai":
            from openai import OpenAI

            self._client = OpenAI(api_key=settings.image_api_key)
        else:
            raise RuntimeError(f"IMAGE_PROVIDER não suportado: {self._provider}")

    def generate(
        self, *, prompt: str, reference_image_base64: str, size: str = "1024x1024"
    ) -> bytes:
        import io

        reference = io.BytesIO(base64.b64decode(reference_image_base64))
        reference.name = "reference.jpg"
        result = self._client.images.edit(
            model=self._model,
            image=reference,
            prompt=prompt,
            size=size,
        )
        b64 = result.data[0].b64_json
        if b64:
            return base64.b64decode(b64)
        # fallback: alguns backends retornam URL em vez de b64
        import urllib.request

        with urllib.request.urlopen(result.data[0].url, timeout=60) as resp:
            return resp.read()


def build_marketing_prompt(
    *,
    image_prompt: str,
    marketing_concept: str = "",
    product: dict[str, Any] | None = None,
) -> str:
    """Monta o prompt final: image_prompt + conceito + dados do produto +
    regras de preservação (sempre por último, para pesar mais)."""
    product = product or {}
    facts = []
    if product.get("name"):
        facts.append(f"Product: {product['name']}")
    if product.get("description"):
        facts.append(f"Description: {product['description']}")
    if product.get("color"):
        facts.append(f"Color (must be preserved): {product['color']}")
    if product.get("size"):
        facts.append(f"Size: {product['size']}")

    parts = []
    if image_prompt:
        parts.append(image_prompt.strip())
    if marketing_concept:
        parts.append(f"Campaign concept: {marketing_concept.strip()}")
    if facts:
        parts.append("Product facts:\n" + "\n".join(facts))
    parts.append(PRESERVATION_RULES)
    return "\n\n".join(parts)


class GenerateMarketingImageTool(BaseTool):
    name = "generate_marketing_image"
    description = (
        "Gera imagem comercial profissional da campanha usando a foto "
        "original como referência (image-to-image), preservando produto, "
        "cor, formato, detalhes e logos."
    )

    def __init__(self, backend: ImageBackend | None = None) -> None:
        self._backend = backend

    def _get_backend(self) -> ImageBackend:
        if self._backend is None:
            self._backend = ApiImageBackend()
        return self._backend

    @retry(
        retry=retry_if_exception_type((RuntimeError, ValueError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _generate(self, prompt: str, reference_b64: str, size: str) -> bytes:
        return self._get_backend().generate(
            prompt=prompt, reference_image_base64=reference_b64, size=size
        )

    def run(
        self,
        *,
        image_prompt: str,
        original_image_base64: str,
        marketing_concept: str = "",
        product: dict[str, Any] | None = None,
        size: str = "1024x1024",
        **_: Any,
    ) -> ToolResult:
        if not image_prompt:
            return ToolResult(
                success=False,
                error="generate_marketing_image requer image_prompt.",
                retryable=False,
            )
        if not original_image_base64:
            return ToolResult(
                success=False,
                error="generate_marketing_image requer a foto original "
                "(original_image_base64).",
                retryable=False,
            )
        try:
            prompt = build_marketing_prompt(
                image_prompt=image_prompt,
                marketing_concept=marketing_concept,
                product=product,
            )
            content = self._generate(prompt, original_image_base64, size)
            if not content:
                raise ValueError("Backend retornou imagem vazia.")
            return ToolResult(
                success=True,
                data={
                    "image_base64": base64.b64encode(content).decode("ascii"),
                    "mime_type": "image/png",
                    "size_bytes": len(content),
                    "prompt_used": prompt,
                    "media_type": "image",  # futuro: video
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("falha na geração de imagem: %s", exc)
            return ToolResult(success=False, error=str(exc), retryable=True)
