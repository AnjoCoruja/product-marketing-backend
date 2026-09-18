"""Image Generation Agent — FASE 6.

Entrada:
  - foto original do produto (base64);
  - dados do produto (Product JSON);
  - conceito da campanha;
  - image_prompt (do Marketing Agent).

Pipeline:
  1. generate_marketing_image — gera a imagem comercial usando a foto
     original como referência (preservando produto, cor, formato,
     detalhes, logos e características visuais);
  2. VALIDAÇÃO — confirma que a imagem foi criada (bytes válidos,
     não vazios) antes de qualquer efeito colateral;
  3. save_marketing_image — salva no Drive, pasta '/fotos geradas',
     nome P000001_C000001_marketing_001.jpg;
  4. update_product_record — grava drive_marketing_url no Sheets;
  5. CampaignRegistry — registra a imagem na campanha (sequencial
     por campanha, pronto para múltiplas imagens por campanha).

Idempotência: rerodar para o mesmo (campaign_id, seq) reutiliza o
arquivo existente no Drive (nome determinístico) e faz upsert no
registry — nunca duplica arquivo nem registro.

Vídeo: NÃO implementado nesta fase (media_type já existe no
contrato para o futuro).
"""

from __future__ import annotations

from typing import Any

from config import get_logger
from services.campaign_registry import CampaignRegistry
from tools.base import BaseTool, ToolResult
from tools.generate_marketing_image import GenerateMarketingImageTool
from tools.product_sheet import UpdateProductRecordTool
from tools.save_marketing_image import SaveMarketingImageTool

logger = get_logger("agent.image_generation")


class ImageGenerationAgent:
    """Orquestra geração → validação → Drive → Sheets → registry."""

    def __init__(
        self,
        generate_tool: GenerateMarketingImageTool | None = None,
        save_tool: SaveMarketingImageTool | None = None,
        update_sheet_tool: UpdateProductRecordTool | None = None,
        campaign_registry: CampaignRegistry | None = None,
        registry_db_path: str = "data/campaign_registry.db",
    ) -> None:
        self.generate_tool = generate_tool or GenerateMarketingImageTool()
        self.save_tool = save_tool or SaveMarketingImageTool()
        self.update_sheet_tool = update_sheet_tool or UpdateProductRecordTool()
        self._registry = campaign_registry or CampaignRegistry(registry_db_path)

    # --- passo 1: validação da imagem gerada ------------------------------

    @staticmethod
    def validate_generated_image(image_base64: str) -> bool:
        """Confirma que a imagem existe e é decodificável/não vazia."""
        if not image_base64:
            return False
        import base64

        try:
            content = base64.b64decode(image_base64)
        except Exception:  # noqa: BLE001
            return False
        return len(content) > 0

    # --- fluxo principal ---------------------------------------------------

    def generate_for_campaign(
        self,
        *,
        campaign_id: str,
        product: dict[str, Any],
        original_image_base64: str,
        image_prompt: str,
        marketing_concept: str = "",
        image_seq: int | None = None,
    ) -> dict[str, Any]:
        """Gera UMA imagem de marketing para a campanha.

        `image_seq` omitido → próximo sequencial da campanha no registry
        (é o que permite múltiplas imagens por campanha no futuro).

        Levanta ValueError em qualquer falha (geração, validação, Drive
        ou Sheets) — o erro descreve o passo.
        """
        product_id = product.get("product_id")
        if not product_id:
            raise ValueError("product sem product_id.")

        seq = image_seq if image_seq is not None else self._registry.next_image_seq(campaign_id)

        # 1. geração (foto original como referência)
        gen = self.generate_tool.run(
            image_prompt=image_prompt,
            original_image_base64=original_image_base64,
            marketing_concept=marketing_concept,
            product=product,
        )
        if not gen.success:
            raise ValueError(f"Geração de imagem falhou: {gen.error}")

        # 2. validação — só prossegue se a imagem foi criada de fato
        if not self.validate_generated_image(gen.data.get("image_base64", "")):
            raise ValueError("Validação falhou: imagem não foi criada (conteúdo vazio).")

        # 3. Drive — pasta '/fotos geradas', nome determinístico
        save = self.save_tool.run(
            product_id=product_id,
            campaign_id=campaign_id,
            image_seq=seq,
            image_base64=gen.data["image_base64"],
            mime_type=gen.data.get("mime_type", "image/jpeg"),
        )
        if not save.success:
            raise ValueError(f"Falha ao salvar no Drive: {save.error}")

        drive_url = save.data.get("web_view_link") or ""

        # 4. Sheets — coluna drive_marketing_url
        sheet = self.update_sheet_tool.run(
            product_id=product_id, updates={"drive_marketing_url": drive_url}
        )
        if not sheet.success:
            raise ValueError(f"Falha ao atualizar o Sheets: {sheet.error}")

        # 5. registry — upsert (idempotente por campaign_id + seq)
        record = self._registry.register_image(
            campaign_id=campaign_id,
            image_seq=seq,
            file_name=save.data["file_name"],
            drive_file_id=save.data.get("file_id"),
            drive_url=drive_url,
            status="generated",
        )

        return {
            "campaign_id": campaign_id,
            "product_id": product_id,
            "image_seq": seq,
            "file_name": save.data["file_name"],
            "drive_file_id": save.data.get("file_id"),
            "drive_marketing_url": drive_url,
            "mime_type": gen.data.get("mime_type"),
            "size_bytes": gen.data.get("size_bytes"),
            "prompt_used": gen.data.get("prompt_used"),
            "media_type": gen.data.get("media_type", "image"),
            "deduplicated": save.data.get("deduplicated", False),
            "registry": record,
        }

    def generate_batch(
        self,
        *,
        campaign_id: str,
        product: dict[str, Any],
        original_image_base64: str,
        image_prompt: str,
        marketing_concept: str = "",
        count: int = 1,
    ) -> list[dict[str, Any]]:
        """Estrutura para múltiplas imagens por campanha (futuro).

        Hoje `count` segue = 1 no uso real; o método já devolve lista e
        usa sequenciais distintos por imagem.
        """
        return [
            self.generate_for_campaign(
                campaign_id=campaign_id,
                product=product,
                original_image_base64=original_image_base64,
                image_prompt=image_prompt,
                marketing_concept=marketing_concept,
            )
            for _ in range(count)
        ]
