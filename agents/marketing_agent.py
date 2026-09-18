"""Marketing Agent — FASE 5.

Entrada:  Product JSON (saída do Product Agent: product_id, name,
          description, color, size, wholesale_price, retail_price).
Saída:    Campaign JSON com campaign_id, product_id, headline,
          marketing_concept, caption_instagram, caption_facebook,
          caption_tiktok, hashtags, image_prompt.

Pipeline determinístico de duas tools:
  1. generate_campaign — LLM cria conceito, headline, copy por
     plataforma, CTA, hashtags e prompt de imagem;
  2. validate_campaign — validação determinística anti-alucinação
     (preços inalterados, sem estoque, sem tamanhos inventados)
     e montagem da Campaign de domínio.

Regra rígida: o agente SÓ usa as informações fornecidas pelo
Product Agent — nunca inventa características, preços, estoque
ou tamanhos.
"""

from typing import Any

from config import get_logger
from schemas import AgentName, AgentState, Campaign, CampaignStatus, Stage
from tools.generate_campaign import GenerateCampaignTool
from tools.validate_campaign import ValidateCampaignTool

from .base import BaseAgent

logger = get_logger("agent.marketing")


class MarketingAgent(BaseAgent):
    name = AgentName.MARKETING

    def __init__(
        self,
        generate_tool: GenerateCampaignTool | None = None,
        validate_tool: ValidateCampaignTool | None = None,
        product_lookup: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.generate_tool = generate_tool or GenerateCampaignTool()
        self.validate_tool = validate_tool or ValidateCampaignTool()
        # callable(product_id) -> dict do produto (default: ProductRegistry)
        self._product_lookup = product_lookup

    def create_campaign(self, product: dict[str, Any]) -> dict[str, Any]:
        """Gera o Campaign JSON completo a partir do Product JSON.

        Levanta ValueError quando a geração falha ou viola as regras
        anti-alucinação (o erro já vem descritivo das tools).
        """
        gen_result = self.generate_tool.run(product=product)
        if not gen_result.success:
            raise ValueError(f"Geração de campanha falhou: {gen_result.error}")

        val_result = self.validate_tool.run(
            generation=gen_result.data, product=product
        )
        if not val_result.success:
            raise ValueError(f"Campanha rejeitada pela validação: {val_result.error}")

        campaign = val_result.data["campaign"]
        return {
            "campaign_id": campaign["campaign_id"],
            "product_id": campaign["product_id"],
            "headline": val_result.data["headline"],
            "marketing_concept": campaign["concept"],
            "caption_instagram": campaign["copy"]["instagram"],
            "caption_facebook": campaign["copy"]["facebook"],
            "caption_tiktok": campaign["copy"]["tiktok"],
            "hashtags": campaign["copy"]["hashtags"],
            "cta": campaign["copy"]["cta"],
            "image_prompt": val_result.data["image_prompt"],
            "status": campaign["status"],
        }

    def _lookup_product(self, product_id: str) -> dict[str, Any]:
        if self._product_lookup is not None:
            return self._product_lookup(product_id)
        from services.product_registry import ProductRegistry

        product = ProductRegistry().get(product_id)
        if not product:
            raise ValueError(f"Produto {product_id} não encontrado no registry.")
        return product

    def _run_stub(self, state: AgentState) -> dict[str, Any]:
        """Fallback da fundação: sem LLM/registry acessível, cria apenas a
        Campaign em DRAFT (sem copy) e segue para aprovação — mesmo
        comportamento do stub da FASE 1."""
        campaign = Campaign(
            product_id=state.product_id or "",
            created_by="initial" if state.run_type.value == "new_product" else "recampaign_08h",
            status=CampaignStatus.DRAFT,
        )
        return {
            "campaign_id": campaign.campaign_id,
            "current_stage": Stage.APPROVAL,
            "active_agent": self.name,
            "pending_human_action": "campaign_approval",
        }

    def run(self, state: AgentState) -> dict[str, Any]:
        """Nó do grafo principal: cria a campanha e encaminha para aprovação."""
        if not state.product_id:
            # fluxo de fundação: Product Agent sem LLM não produziu id
            return self._run_stub(state)
        try:
            product = self._lookup_product(state.product_id)
            campaign_json = self.create_campaign(product)
        except ValueError as exc:
            self.logger.warning("marketing_agent em modo stub: %s", exc)
            return self._run_stub(state)
        return {
            "campaign_id": campaign_json["campaign_id"],
            "current_stage": Stage.APPROVAL,
            "active_agent": self.name,
            "pending_human_action": "campaign_approval",
            "campaign": campaign_json,
        }
